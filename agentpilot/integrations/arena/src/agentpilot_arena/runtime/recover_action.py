"""Safely recover one externally interrupted AgentPilot Arena action claim.

The action journal intentionally does not recycle ``claimed`` rows on lease
expiry: an evaluator may still be running.  This operator tool provides the
missing explicit path for a process that was killed before it could persist a
terminal result.  Uncommitted work is removed and released; a prepared commit
is moved to the single post-commit resume state without deleting its worktree.
The operation is fail-closed and requires the configured Docker daemon to have
no running inner containers.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import tomllib

from agentpilot_arena.runtime.git_workspace import GitWorkspace


class RecoveryRefused(RuntimeError):
    """One of the stale-claim recovery fences did not hold."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise RecoveryRefused(f"{name} must be a TOML table")
    return value


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecoveryRefused(f"{name} must be non-empty text")
    return value.strip()


def _absolute_path(value: Any, name: str) -> Path:
    path = Path(_text(value, name)).expanduser()
    if not path.is_absolute():
        raise RecoveryRefused(f"{name} must be absolute")
    return path


def _run(
    arguments: Sequence[str],
    *,
    cwd: Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(arguments),
            cwd=None if cwd is None else str(cwd),
            env=None if environment is None else dict(environment),
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RecoveryRefused(f"cannot run {arguments[0]!r}") from exc


def _load_config(path: Path) -> Mapping[str, Any]:
    try:
        with path.open("rb") as stream:
            config = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise RecoveryRefused(f"cannot read AgentPilot Arena config: {path}") from exc
    environment = _mapping(config.get("environment"), "environment")
    settings = _mapping(environment.get("settings"), "environment.settings")
    factory = environment.get("factory")
    if factory not in {
        "agentpilot_arena.aime:create_dependencies",
        "agentpilot_arena.tb2:create_dependencies",
    }:
        raise RecoveryRefused("config does not select an AgentPilot Arena environment")
    _mapping(settings.get("action_runtime"), "environment.settings.action_runtime")
    _mapping(settings.get("cell_runtime"), "environment.settings.cell_runtime")
    try:
        from agentpilot.app import load_app_config

        parsed = load_app_config(path)
        if factory == "agentpilot_arena.aime:create_dependencies":
            from agentpilot_arena.aime.factory import create_factory

            create_factory().preflight_config(parsed)
        else:
            from agentpilot_arena.tb2.factory import create_factory

            create_factory().preflight_config(parsed)
    except Exception as exc:
        raise RecoveryRefused(
            "config fails the selected AgentPilot Arena production preflight"
        ) from exc
    return config


def _action_settings(config: Mapping[str, Any]) -> Mapping[str, Any]:
    environment = _mapping(config["environment"], "environment")
    settings = _mapping(environment["settings"], "environment.settings")
    return _mapping(settings.get("action_runtime"), "action_runtime")


def _expired_run_lease(
    database_path: Path, run_id: str, now: datetime
) -> Mapping[str, object]:
    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT batch_id, owner_token, fencing_token, acquired_at, "
                "renewed_at, expires_at FROM run_batch_leases WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise RecoveryRefused("cannot inspect the AgentPilot run lease") from exc
    if row is None:
        raise RecoveryRefused("the requested run has no durable lease row")
    try:
        expires_at = datetime.fromisoformat(row[5]).astimezone(timezone.utc)
    except (TypeError, ValueError) as exc:
        raise RecoveryRefused("the durable run lease has an invalid expiry") from exc
    if expires_at >= now:
        raise RecoveryRefused(f"run lease remains live until {expires_at.isoformat()}")
    return {
        "run_id": run_id,
        "batch_id": row[0],
        "owner_token": row[1],
        "fencing_token": row[2],
        "acquired_at": row[3],
        "renewed_at": row[4],
        "expires_at": expires_at.isoformat(),
    }


def _ancestor_pids() -> set[int]:
    ancestors: set[int] = set()
    current = os.getpid()
    while current > 1 and current not in ancestors:
        ancestors.add(current)
        try:
            fields = Path(f"/proc/{current}/stat").read_text().split()
            current = int(fields[3])
        except (OSError, ValueError, IndexError):
            break
    return ancestors


def _related_processes(needles: Sequence[str], worktree: Path | None) -> list[str]:
    ignored = _ancestor_pids()
    matches: list[str] = []
    uid = os.getuid()
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in ignored:
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            command = (
                (entry / "cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode("utf-8", errors="replace")
            )
            cwd = (entry / "cwd").resolve()
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        command_match = any(needle and needle in command for needle in needles)
        cwd_match = worktree is not None and (
            cwd == worktree or cwd.is_relative_to(worktree)
        )
        if command_match or cwd_match:
            matches.append(f"pid={pid} cwd={cwd} command={command[:500]}")
    return matches


def _docker_inventory(
    executable: str, docker_host: str
) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    environment = os.environ.copy()
    environment["DOCKER_HOST"] = docker_host
    completed = _run(
        [
            executable,
            "ps",
            "-a",
            "--format",
            "{{json .}}",
        ],
        environment=environment,
    )
    if completed.returncode != 0:
        raise RecoveryRefused(
            f"cannot inspect configured Docker daemon: {completed.stderr.strip()}"
        )
    all_containers: list[dict[str, str]] = []
    for line in completed.stdout.splitlines():
        if line.strip():
            value = json.loads(line)
            if not isinstance(value, dict):
                raise RecoveryRefused("Docker inventory returned a malformed row")
            all_containers.append({str(key): str(item) for key, item in value.items()})
    running = [
        row
        for row in all_containers
        if not row.get("State", "").lower().startswith("exited")
        and row.get("State", "").lower() not in {"dead", "created"}
    ]
    return all_containers, running


def _git(repository: Path, *arguments: str) -> str:
    completed = _run(["git", "-C", str(repository), *arguments])
    if completed.returncode != 0:
        raise RecoveryRefused(f"git {arguments[0]} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def _validate_worktree(
    *,
    repository: Path,
    worktrees_root: Path,
    worktree_id: str | None,
    worktree_path: str | None,
    parent_commit_sha: str,
    result_commit_sha: str | None,
) -> tuple[GitWorkspace | None, Mapping[str, Any]]:
    if (worktree_id is None) is not (worktree_path is None):
        raise RecoveryRefused("journal workspace coordinates are incomplete")
    if worktree_id is None:
        return None, {"present": False, "journal_coordinates": False}
    path = Path(worktree_path).resolve(strict=False)
    root = worktrees_root.resolve()
    if path.parent != root or path.name != worktree_id:
        raise RecoveryRefused("journal worktree is outside its configured root")
    if not path.exists() or not path.is_dir() or path.is_symlink():
        registered = _git(repository, "worktree", "list", "--porcelain")
        if f"worktree {path}" in registered:
            raise RecoveryRefused("missing worktree remains registered in Git")
        return None, {
            "present": False,
            "journal_coordinates": True,
            "path": str(path),
        }
    head = _git(path, "rev-parse", "HEAD").lower()
    if head != parent_commit_sha.lower():
        raise RecoveryRefused(
            "interrupted worktree HEAD differs from the action parent commit"
        )
    status = _git(path, "status", "--short", "--untracked-files=all").splitlines()
    if result_commit_sha is not None:
        resolved = _git(repository, "rev-parse", f"{result_commit_sha}^{{commit}}")
        parent = _git(repository, "rev-parse", f"{result_commit_sha}^")
        prepared_tree = _git(repository, "rev-parse", f"{result_commit_sha}^{{tree}}")
        staged_tree = _git(path, "write-tree")
        if (
            resolved.lower() != result_commit_sha.lower()
            or parent.lower() != parent_commit_sha.lower()
            or prepared_tree.lower() != staged_tree.lower()
        ):
            raise RecoveryRefused(
                "prepared candidate commit differs from the owned worktree"
            )
    workspace = GitWorkspace(worktree_id, path, parent_commit_sha.lower())
    return workspace, {
        "present": True,
        "journal_coordinates": True,
        "path": str(path),
        "head": head,
        "dirty_paths": status,
        "prepared_commit": result_commit_sha,
    }


def _write_audit(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _private_action_inventory(
    root: Path, action_id: str
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    from agentpilot_arena.runtime.action_connectors import (
        ACTION_PROVIDER_ACCOUNTING_SUFFIX,
    )

    suffix = hashlib.sha256(action_id.encode("utf-8")).hexdigest()[:24]
    resolved_root = root.resolve()
    paths = tuple(sorted(resolved_root.glob(f"action-{suffix}-*")))
    journals: list[Path] = []
    homes: list[Path] = []
    for path in paths:
        if path.parent != resolved_root or path.is_symlink():
            raise RecoveryRefused("private action path is invalid")
        if path.name.endswith(ACTION_PROVIDER_ACCOUNTING_SUFFIX):
            if not path.is_file():
                raise RecoveryRefused("provider accounting journal is invalid")
            journals.append(path)
        elif not path.is_dir():
            raise RecoveryRefused("private action home must be a directory")
        else:
            homes.append(path)
    return tuple(homes), tuple(journals)


def _private_home_cleanup_plan(root: Path, action_id: str) -> tuple[Path, ...]:
    """Validate every private action path without changing the filesystem."""

    homes, journals = _private_action_inventory(root, action_id)
    if journals:
        raise RecoveryRefused(
            "provider accounting journal forbids uncommitted action replay"
        )
    return homes


def _interrupted_accounting(
    journals: Sequence[Path],
) -> tuple[Mapping[str, object], float | None]:
    from agentpilot_arena.runtime.action_connectors import BoundedCodexCliHarness
    from agentpilot_arena.runtime.action_provider_broker import (
        ActionProviderAccountingSnapshot,
    )

    snapshots = tuple(
        BoundedCodexCliHarness._provider_accounting_snapshot(path) for path in journals
    )
    if not snapshots:
        raise RecoveryRefused("provider accounting journal set is empty")
    complete = all(
        snapshot.accounting_status == "complete" and snapshot.cost_usd is not None
        for snapshot in snapshots
    )
    settled_values = tuple(snapshot.settled_cost_usd for snapshot in snapshots)
    settled_completed_cost = sum(
        float(value) for value in settled_values if value is not None
    )
    reasons = tuple(
        dict.fromkeys(
            reason for snapshot in snapshots for reason in snapshot.quarantine_reasons
        )
    )
    if not complete and not reasons:
        reasons = ("interrupted_action_accounting_incomplete",)
    aggregate = ActionProviderAccountingSnapshot(
        requests=sum(snapshot.requests for snapshot in snapshots),
        request_bytes=sum(snapshot.request_bytes for snapshot in snapshots),
        response_bytes=sum(snapshot.response_bytes for snapshot in snapshots),
        input_tokens=sum(snapshot.input_tokens for snapshot in snapshots),
        cache_tokens=sum(snapshot.cache_tokens for snapshot in snapshots),
        output_tokens=sum(snapshot.output_tokens for snapshot in snapshots),
        cost_usd=(
            sum(float(snapshot.cost_usd) for snapshot in snapshots)
            if complete
            else None
        ),
        accounting_status="complete" if complete else "quarantined",
        response_ids=tuple(
            value for snapshot in snapshots for value in snapshot.response_ids
        ),
        generation_ids=tuple(
            value for snapshot in snapshots for value in snapshot.generation_ids
        ),
        quarantine_reasons=reasons,
        settled_cost_usd=(
            sum(float(value) for value in settled_values)
            if all(value is not None for value in settled_values)
            else None
        ),
    )
    diagnostics: Mapping[str, object] = {
        "disposition": "accounting_quarantine",
        "reason": "interrupted_after_provider_dispatch",
        "provider_accounting": aggregate.as_dict(),
        "provider_accounting_invocations": [
            {
                "journal": path.name,
                **snapshot.as_dict(),
            }
            for path, snapshot in zip(journals, snapshots, strict=True)
        ],
        "settled_completed_invocation_cost_usd": settled_completed_cost,
    }
    return diagnostics, aggregate.cost_usd


def _remove_private_home(root: Path, action_id: str) -> tuple[Path, ...]:
    paths = _private_home_cleanup_plan(root, action_id)
    for path in paths:
        shutil.rmtree(path)
    return paths


def _remove_exited_containers(
    executable: str, docker_host: str, inventory: Sequence[Mapping[str, str]]
) -> list[str]:
    identifiers = [
        row.get("ID", "")
        for row in inventory
        if row.get("State", "").lower().startswith("exited")
    ]
    identifiers = [identifier for identifier in identifiers if identifier]
    if not identifiers:
        return []
    environment = os.environ.copy()
    environment["DOCKER_HOST"] = docker_host
    completed = _run(
        [executable, "rm", *identifiers],
        environment=environment,
    )
    if completed.returncode != 0:
        raise RecoveryRefused(
            f"cannot remove exited containers: {completed.stderr.strip()}"
        )
    return identifiers


def recover(arguments: argparse.Namespace) -> Mapping[str, Any]:
    # Keep optional method dependencies lazy so ``--help`` remains available.
    global ActionRecordStatus, SQLiteActionStore, GitWorkspaceManager
    from agentpilot_arena.runtime.action_store import (
        ActionRecordStatus,
        SQLiteActionStore,
        process_identity_is_live,
    )
    from agentpilot_arena.runtime.git_workspace import GitWorkspaceManager

    now = datetime.now(timezone.utc)
    config_path = arguments.config.expanduser().resolve()
    config = _load_config(config_path)
    action = _action_settings(config)
    runtime = _mapping(config.get("runtime"), "runtime")
    settings = _mapping(
        _mapping(config["environment"], "environment")["settings"],
        "environment.settings",
    )
    cell = _mapping(settings.get("cell_runtime"), "cell_runtime")
    codex = _mapping(action.get("codex"), "action_runtime.codex")

    action_store_path = _absolute_path(
        action.get("action_store_path"), "action_runtime.action_store_path"
    )
    program_repository = _absolute_path(
        action.get("program_repository"), "action_runtime.program_repository"
    )
    worktrees_root = _absolute_path(
        action.get("worktrees_root"), "action_runtime.worktrees_root"
    )
    codex_home_root = _absolute_path(
        codex.get("codex_home_root"), "action_runtime.codex.codex_home_root"
    )
    runtime_database = _absolute_path(
        runtime.get("database_path"), "runtime.database_path"
    )
    docker_executable = str(
        _absolute_path(cell.get("docker_executable"), "cell_runtime.docker_executable")
    )
    docker_host = _text(cell.get("docker_host"), "cell_runtime.docker_host")

    store = SQLiteActionStore.open_for_recovery(action_store_path)
    record = store.get(arguments.action_id)
    if record is None:
        raise RecoveryRefused("action journal has no matching action")
    interrupted_code = (
        "action_execution_failed:interrupted_postcommit:ProcessTerminated"
    )
    already_blocked = (
        record.status is ActionRecordStatus.BLOCKED
        and record.result_commit_sha is not None
        and record.failure_code == interrupted_code
    )
    if record.status is not ActionRecordStatus.CLAIMED and not already_blocked:
        raise RecoveryRefused(f"action status is {record.status.value}, not claimed")
    if not already_blocked and record.claim_token is None:
        raise RecoveryRefused("claimed action has no claim token")
    age = (now - record.claimed_at).total_seconds()
    if age < arguments.minimum_age_seconds:
        raise RecoveryRefused(
            f"claim is only {age:.1f}s old; minimum is {arguments.minimum_age_seconds:.1f}s"
        )
    expired_lease = _expired_run_lease(runtime_database, arguments.run_id, now)
    context = record.claim_context
    if (
        context.action_id,
        context.run_id,
        context.batch_id,
        context.owner_token,
        context.fencing_token,
        context.parent_commit_sha.lower(),
    ) != (
        record.action_id,
        arguments.run_id,
        expired_lease["batch_id"],
        expired_lease["owner_token"],
        expired_lease["fencing_token"],
        arguments.parent_commit_sha.lower(),
    ):
        raise RecoveryRefused(
            "action claim is not owned by the exact expired run fence"
        )
    if process_identity_is_live(context):
        raise RecoveryRefused("the exact action executor process is still alive")

    worktree = None if record.worktree_path is None else Path(record.worktree_path)
    related = _related_processes(
        (
            arguments.action_id,
            arguments.run_id,
            str(config_path),
            str(action_store_path),
        ),
        worktree,
    )
    if related:
        raise RecoveryRefused(
            "related processes are still alive:\n" + "\n".join(related)
        )
    containers, running = _docker_inventory(docker_executable, docker_host)
    if running:
        names = ", ".join(row.get("Names", row.get("ID", "?")) for row in running)
        raise RecoveryRefused(
            f"configured Docker still has running containers: {names}"
        )

    workspace, workspace_audit = _validate_worktree(
        repository=program_repository,
        worktrees_root=worktrees_root,
        worktree_id=record.worktree_id,
        worktree_path=record.worktree_path,
        parent_commit_sha=arguments.parent_commit_sha,
        result_commit_sha=record.result_commit_sha,
    )
    private_homes, provider_journals = _private_action_inventory(
        codex_home_root, record.action_id
    )
    audit_path = (
        arguments.audit_path.expanduser().resolve()
        if arguments.audit_path is not None
        else action_store_path.with_name(
            f"{action_store_path.stem}.recovery-{now.strftime('%Y%m%dT%H%M%SZ')}.json"
        )
    )
    audit: dict[str, Any] = {
        "prepared_at": now.isoformat(),
        "config_path": str(config_path),
        "run_id": arguments.run_id,
        "expired_lease": expired_lease,
        "action_id": record.action_id,
        "claim_token": record.claim_token,
        "claimed_at": record.claimed_at.isoformat(),
        "claim_age_seconds": age,
        "result_commit_sha": record.result_commit_sha,
        "claim_context": {
            "fence_id": context.fence_id,
            "runner_id": context.runner_id,
            "scope_identity": context.scope_identity,
            "process_id": context.process_id,
            "process_start_ticks": context.process_start_ticks,
        },
        "workspace": workspace_audit,
        "private_action_homes": [str(path) for path in private_homes],
        "provider_accounting_journals": [str(path) for path in provider_journals],
        "docker_inventory": containers,
        "related_processes": related,
        "status": "prepared",
    }
    _write_audit(audit_path, audit)

    if record.result_commit_sha is None:
        if provider_journals:
            diagnostics, known_cost = _interrupted_accounting(provider_journals)
            store.block_stale_claimed_uncommitted_accounting(
                record=record,
                failure_code=(
                    "action_execution_failed:interrupted_precommit:"
                    "ProviderAccountingPresent"
                ),
                failure_diagnostics=diagnostics,
                failure_cost_usd=known_cost,
            )
            removed_home = None
            recovery_status = "blocked_for_accounting_quarantine"
            audit["provider_accounting"] = diagnostics
        else:
            # Make every refusal decision before deleting the candidate
            # worktree. A failed recovery must leave the stale claim intact.
            _private_home_cleanup_plan(codex_home_root, record.action_id)
            if workspace is not None:
                GitWorkspaceManager(
                    repository=program_repository,
                    worktrees_root=worktrees_root,
                ).remove(workspace)
            removed_home = _remove_private_home(codex_home_root, record.action_id)
            store.release_stale_claimed_uncommitted(
                action_id=record.action_id,
                expected_claim_token=record.claim_token,
                expected_claimed_at=record.claimed_at,
                expected_worktree_id=record.worktree_id,
                expected_worktree_path=record.worktree_path,
            )
            recovery_status = "released"
    else:
        if workspace is None or record.committed_implementation is None:
            raise RecoveryRefused(
                "committed action lacks its worktree or implementation facts"
            )
        if not already_blocked:
            store.block_stale_claimed_postcommit(
                record=record,
                failure_code=interrupted_code,
            )
        removed_home = None
        recovery_status = (
            "already_blocked_for_postcommit_resume"
            if already_blocked
            else "blocked_for_postcommit_resume"
        )
    removed_containers = (
        _remove_exited_containers(docker_executable, docker_host, containers)
        if arguments.remove_exited_containers
        else []
    )
    audit.update(
        {
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "status": recovery_status,
            "removed_private_home": (
                None if removed_home is None else [str(path) for path in removed_home]
            ),
            "removed_exited_container_ids": removed_containers,
        }
    )
    _write_audit(audit_path, audit)
    return {"audit_path": str(audit_path), **audit}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--action-id", required=True)
    parser.add_argument("--parent-commit-sha", required=True)
    parser.add_argument("--minimum-age-seconds", type=float, default=3600.0)
    parser.add_argument("--audit-path", type=Path)
    parser.add_argument("--remove-exited-containers", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    if arguments.minimum_age_seconds <= 0:
        raise SystemExit("--minimum-age-seconds must be positive")
    try:
        result = recover(arguments)
    except RecoveryRefused as exc:
        print(f"recovery refused: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
