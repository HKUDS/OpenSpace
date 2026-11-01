"""Launch and resume AgentPilot campaigns inside Arena's native authority."""

# Persistent format and evaluation identifiers retain their historical values.

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep

from agentpilot.app.campaign_driver import (
    CampaignOutcome,
    CampaignStatus,
    CampaignSupervisor,
)
from agentpilot.app.contracts import BatchExecutionInterrupted
from agentpilot.app.runtime_config import AppConfig, RuntimeConfigError, load_app_config
from agentpilot.persistence import SQLiteUnitOfWorkFactory
from agentpilot.persistence.terminal_archive import derive_terminal_archive_coordinates
from rsi_arena import (
    ArenaSession,
    load_experiment_toml,
    open_native_session,
    resume_native_session,
)
from rsi_arena.held_out_evaluation import run_held_out_evaluation
from rsi_arena.projection import open_run_replay

from .arena_companion import verify_authority_pair
from .native import AgentPilotArenaMethodRunner
from .runtime.arena_paired import arena_occurrence_key

_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_AUTHORITY_BINDING = "agentpilot-authority.json"
_EXIT_CODES = {
    CampaignStatus.COMPLETED: 0,
    CampaignStatus.PLANNING_BUDGET_EXHAUSTED: 76,
    CampaignStatus.ACCOUNTING_QUARANTINE: 77,
    CampaignStatus.TERMINAL_FAILURE: 78,
    CampaignStatus.STALLED: 79,
}


def _load_env_file(path: str | Path) -> None:
    """Load an explicit dotenv file without replacing host-owned values."""

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


@dataclass
class _SessionBatchInvoker:
    """Run campaign batches against one exclusively open Arena session."""

    session: ArenaSession
    config: AppConfig
    benchmark_id: str
    run_id: str
    terminal_status: CampaignStatus | None = field(default=None, init=False)
    terminal_reason: str | None = field(default=None, init=False)

    def start(self, *, batch_id: str, rounds: int) -> int:
        return self._invoke(batch_id=batch_id, rounds=rounds, resume=False)

    def resume(self, *, batch_id: str) -> int:
        return self._invoke(batch_id=batch_id, rounds=1, resume=True)

    def _invoke(self, *, batch_id: str, rounds: int, resume: bool) -> int:
        self.terminal_status = None
        self.terminal_reason = None
        try:
            AgentPilotArenaMethodRunner(
                config=self.config,
                benchmark_id=self.benchmark_id,
                run_id=self.run_id,
                batch_id=batch_id,
                rounds=rounds,
                resume=resume,
            ).run(self.session)
        except Exception as exc:  # noqa: BLE001 - CLI error boundary
            if isinstance(exc, BatchExecutionInterrupted) and any(
                isinstance(getattr(error, "diagnostics", None), Mapping)
                and getattr(error, "diagnostics").get("disposition")
                == "accounting_quarantine"
                for error in exc.errors.values()
            ):
                self.terminal_status = CampaignStatus.ACCOUNTING_QUARANTINE
                self.terminal_reason = "action provider accounting is quarantined"
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "native_batch_failed",
                            "type": type(exc).__qualname__,
                        },
                        "batch_id": batch_id,
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
                flush=True,
            )
            return 1
        return 0


class _CampaignIncomplete(RuntimeError):
    def __init__(self, outcome: CampaignOutcome) -> None:
        super().__init__(outcome.status.value)
        self.outcome = outcome


def _validate_session(
    session: ArenaSession, benchmark_id: str, config: AppConfig
) -> None:
    active = tuple(session.development_task_ids)
    if active != (benchmark_id,):
        raise RuntimeConfigError(
            "the Arena run must contain exactly the selected AgentPilot benchmark"
        )
    resolved = session.spine.configs.get(benchmark_id)
    plan = getattr(resolved, "plan", None)
    expected_method = f"agentpilot-{config.policy.method.value}"
    if getattr(plan, "method", None) != expected_method:
        raise RuntimeConfigError(
            "the Arena run method differs from the AgentPilot policy"
        )


def _file_digest(path: Path) -> str:
    try:
        return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeConfigError("cannot read an authority binding input") from exc


def _binding_payload(
    arguments: argparse.Namespace, config: AppConfig
) -> dict[str, object]:
    run_root = Path(arguments.arena_run_root).expanduser().resolve()
    return {
        "schema": "openthink-arena-authority-binding",
        "run_id": arguments.run_id,
        "benchmark": arguments.benchmark,
        "policy_method": config.policy.method.value,
        "environment_identity": config.environment.identity,
        "database_path": str(config.runtime.database_path),
        "config_digest": _file_digest(Path(arguments.config).expanduser()),
        "arena_experiment_digest": _file_digest(run_root / "experiment.json"),
    }


def _prepare_runtime_parents(config: AppConfig) -> None:
    """Create explicit SQLite parents before an Arena authority is opened."""

    targets = [Path(config.runtime.database_path)]
    settings = config.environment.settings
    action = settings.get("action_runtime") if isinstance(settings, Mapping) else None
    action_store = (
        action.get("action_store_path") if isinstance(action, Mapping) else None
    )
    if action_store is not None:
        targets.append(Path(str(action_store)).expanduser())
    for target in targets:
        if not target.is_absolute():
            raise RuntimeConfigError("runtime database paths must be absolute")
        parent = target.parent
        if parent.exists() and (parent.is_symlink() or not parent.is_dir()):
            raise RuntimeConfigError("runtime database parent is not a real directory")
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)


def _write_authority_binding(arguments: argparse.Namespace, config: AppConfig) -> None:
    root = Path(arguments.arena_run_root).expanduser().resolve()
    path = root / _AUTHORITY_BINDING
    raw = (
        json.dumps(
            _binding_payload(arguments, config),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise RuntimeConfigError("cannot create the AgentPilot Arena binding") from exc


def _verify_authority_binding(arguments: argparse.Namespace, config: AppConfig) -> None:
    root = Path(arguments.arena_run_root).expanduser().resolve()
    path = root / _AUTHORITY_BINDING
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            actual = json.load(stream)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeConfigError("AgentPilot Arena binding is unavailable") from exc
    if actual != _binding_payload(arguments, config):
        raise RuntimeConfigError("AgentPilot Arena binding differs from this resume")


def _program_repository(config: AppConfig) -> Path:
    settings = config.environment.settings
    action = settings.get("action_runtime") if isinstance(settings, Mapping) else None
    value = action.get("program_repository") if isinstance(action, Mapping) else None
    try:
        repository = Path(str(value)).expanduser().resolve(strict=True)
    except OSError as exc:
        raise RuntimeConfigError("program repository is unavailable") from exc
    if value is None or not repository.is_dir():
        raise RuntimeConfigError("program repository is unavailable")
    root_seed = settings.get("root_seed") if isinstance(settings, Mapping) else None
    cell = settings.get("cell_runtime") if isinstance(settings, Mapping) else None
    expected_commit = (
        root_seed.get("commit_sha") if isinstance(root_seed, Mapping) else None
    )
    expected_tree = (
        cell.get("trusted_harbor_tree") if isinstance(cell, Mapping) else None
    )
    environment = {
        "PATH": os.environ.get("PATH", os.defpath),
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "LC_ALL": "C",
    }

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ("git", *arguments),
            cwd=repository,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise RuntimeConfigError("program repository Git authority is invalid")
        return completed.stdout.strip()

    try:
        top = Path(git("rev-parse", "--show-toplevel")).resolve(strict=True)
    except OSError as exc:
        raise RuntimeConfigError("program repository Git root is unavailable") from exc
    commit = git("rev-parse", "HEAD").lower()
    tree = git("rev-parse", "HEAD^{tree}").lower()
    status = git("status", "--porcelain=v1", "--untracked-files=all", "--ignored")
    if (
        top != repository
        or not isinstance(expected_commit, str)
        or commit != expected_commit.lower()
        or not isinstance(expected_tree, str)
        or tree != expected_tree.lower()
        or status
    ):
        raise RuntimeConfigError(
            "program repository differs from the clean configured root commit"
        )
    return repository


def _commit_tree(repository: Path, commit_sha: str) -> str:
    completed = subprocess.run(
        ("git", "rev-parse", f"{commit_sha}^{{tree}}"),
        cwd=repository,
        env={
            "PATH": os.environ.get("PATH", os.defpath),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "LC_ALL": "C",
        },
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    tree = completed.stdout.strip().lower()
    if (
        completed.returncode != 0
        or re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", tree) is None
    ):
        raise RuntimeConfigError("champion commit tree is unavailable")
    return tree


@contextmanager
def _open_launch_session(
    experiment: Mapping[str, object],
    *,
    run_root: str | Path,
    trusted_candidate_root: Path,
) -> Iterator[ArenaSession]:
    """Open Arena with an empty method workspace, not the candidate Git repo.

    AgentPilot materializes every evaluated commit through its frozen
    ``program_repository``.  Copying that repository into Arena's method
    workspace is redundant and would incorrectly subject tracked repository
    symlinks to the method-workspace snapshot boundary.
    """

    with TemporaryDirectory(prefix="agentpilot-arena-method-") as raw:
        workspace = Path(raw) / "workspace"
        workspace.mkdir(mode=0o700)
        with open_native_session(
            experiment,
            run_root=run_root,
            workspace_root=workspace,
            trusted_candidate_root=trusted_candidate_root,
        ) as session:
            yield session


def _supervise(
    session: ArenaSession,
    *,
    config: AppConfig,
    benchmark_id: str,
    run_id: str,
) -> CampaignOutcome:
    if config.campaign is None:
        raise RuntimeConfigError("native campaign requires a [campaign] table")
    _validate_session(session, benchmark_id, config)
    unit_of_work = SQLiteUnitOfWorkFactory(
        config.runtime.database_path,
        timeout=config.runtime.sqlite_timeout_seconds,
    )
    return CampaignSupervisor(
        unit_of_work=unit_of_work,
        lease_manager=unit_of_work.lease_manager(),
        invoker=_SessionBatchInvoker(session, config, benchmark_id, run_id),
        config=config.campaign,
        run_id=run_id,
        sleep=sleep,
        log=lambda message: print(message, flush=True),
    ).run()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agentpilot-arena",
        description="Run an AgentPilot campaign through Arena native paired attempts.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser(
        "launch", help="create the Arena authority and start a campaign"
    )
    resume = commands.add_parser(
        "resume", help="resume only from an existing Arena authority"
    )
    finalize = commands.add_parser(
        "finalize", help="select the durable champion and run sealed held-out C"
    )
    for command in (launch, resume, finalize):
        command.add_argument("--config", required=True)
        command.add_argument("--arena-run-root", required=True)
        command.add_argument("--benchmark", choices=("aime", "tb2"), required=True)
        command.add_argument("--run-id", required=True)
        command.add_argument("--env-file")
    finalize.add_argument("--retry-inconclusive", action="store_true")
    return parser


def _outcome_payload(outcome: CampaignOutcome, *, run_id: str) -> Mapping[str, object]:
    payload: dict[str, object] = {
        "run_id": run_id,
        "status": outcome.status.value,
        "reason": outcome.reason,
        "completed_processes": outcome.completed_processes,
    }
    if outcome.batch_id is not None:
        payload["batch_id"] = outcome.batch_id
    return payload


def _run(arguments: argparse.Namespace) -> CampaignOutcome:
    if arguments.env_file is not None:
        _load_env_file(arguments.env_file)
    config = load_app_config(arguments.config)
    _prepare_runtime_parents(config)
    if arguments.command == "launch":
        experiment = load_experiment_toml(verify_authority_pair(arguments.config))
        context = _open_launch_session(
            experiment,
            run_root=arguments.arena_run_root,
            trusted_candidate_root=_program_repository(config),
        )
    else:
        _verify_authority_binding(arguments, config)
        context = resume_native_session(run_root=arguments.arena_run_root)
    try:
        with context as session:
            if arguments.command == "launch":
                _validate_session(session, arguments.benchmark, config)
                _write_authority_binding(arguments, config)
            outcome = _supervise(
                session,
                config=config,
                benchmark_id=arguments.benchmark,
                run_id=arguments.run_id,
            )
            if outcome.status is not CampaignStatus.COMPLETED:
                # Arena runs held-out evaluation only after a normally completed
                # native method context. A stopped campaign must remain resumable.
                raise _CampaignIncomplete(outcome)
            return outcome
    except _CampaignIncomplete as stopped:
        return stopped.outcome


def _finalize(arguments: argparse.Namespace) -> Mapping[str, object]:
    """Map AgentPilot's durable champion to one delivered Arena challenger."""

    if arguments.env_file is not None:
        _load_env_file(arguments.env_file)
    config = load_app_config(arguments.config)
    _verify_authority_binding(arguments, config)
    unit_of_work = SQLiteUnitOfWorkFactory(
        config.runtime.database_path,
        timeout=config.runtime.sqlite_timeout_seconds,
    )
    now = datetime.now(timezone.utc)
    with unit_of_work() as work:
        programs = work.search_read_model.list_programs_for_run(arguments.run_id)
        coordinates = derive_terminal_archive_coordinates(
            run_id=arguments.run_id,
            batches=work.batches.list_for_run(arguments.run_id),
            programs=programs,
            as_of=now,
        )
    champions = tuple(
        record
        for record in programs
        if record.parent.candidate.parent_id == coordinates.champion_program_id
    )
    if len(champions) != 1:
        raise RuntimeConfigError("durable champion program is not unique")
    champion_commit = champions[0].parent.candidate.commit_sha
    champion_tree = _commit_tree(_program_repository(config), champion_commit)
    source_action_id = champions[0].source_action_id
    if source_action_id is None:
        raise RuntimeConfigError("durable champion has no source action")
    expected_occurrence = arena_occurrence_key(source_action_id, "challenger")

    replay, _experiment, _authority = open_run_replay(arguments.arena_run_root)
    delivered_candidate_ids = {
        terminal.candidate_id for terminal in replay.delivered_results
    }
    candidates = tuple(
        candidate
        for candidate in replay.candidates
        if candidate.candidate_id in delivered_candidate_ids
        and candidate.role == "challenger"
        and candidate.native_candidate_id == expected_occurrence
    )
    if len(candidates) != 1:
        raise RuntimeConfigError(
            "durable champion has no unique delivered Arena challenger"
        )
    if candidates[0].tree_sha != champion_tree:
        raise RuntimeConfigError("durable champion and Arena candidate trees differ")
    candidate_id = candidates[0].candidate_id

    report = run_held_out_evaluation(
        run_root=arguments.arena_run_root,
        candidate_id=candidate_id,
        retry_inconclusive=bool(arguments.retry_inconclusive),
    )
    return {
        "run_id": arguments.run_id,
        "benchmark": arguments.benchmark,
        "champion_program_id": coordinates.champion_program_id,
        "champion_commit_sha": champion_commit,
        "arena_candidate_id": candidate_id,
        "held_out_report": report,
    }


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.command == "finalize":
        try:
            payload = _finalize(arguments)
        except Exception as exc:  # noqa: BLE001 - CLI error boundary
            print(
                json.dumps(
                    {
                        "error": {
                            "code": "native_finalization_failed",
                            "type": type(exc).__qualname__,
                        }
                    },
                    sort_keys=True,
                ),
                file=sys.stderr,
            )
            return 1
        print(json.dumps(payload, sort_keys=True))
        return 0
    try:
        outcome = _run(arguments)
    except Exception as exc:  # noqa: BLE001 - CLI error boundary
        print(
            json.dumps(
                {
                    "error": {
                        "code": "native_campaign_failed",
                        "type": type(exc).__qualname__,
                    }
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1
    payload = _outcome_payload(outcome, run_id=arguments.run_id)
    stream = sys.stdout if outcome.status is CampaignStatus.COMPLETED else sys.stderr
    print(json.dumps(payload, sort_keys=True), file=stream)
    return _EXIT_CODES[outcome.status]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = ["main"]
