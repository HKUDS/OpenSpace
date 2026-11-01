"""Read-only HTTP surface for the run inspector.

The server owns no runtime state: every response is a projection built through
the same read-only unit of work the CLI export uses.  It never opens a writable
connection, so browsing a run cannot perturb it, and it binds to the loopback
interface because the payload contains untrusted model text.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import tomllib
from collections.abc import Sequence
from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from agentpilot.persistence import SQLiteReadOnlyUnitOfWorkFactory
from agentpilot.webui.read_model import RunViewAdapter

_STATIC_ROOT = Path(__file__).resolve().parent / "static"
_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".ttf": "font/ttf",
}
_SHA_RE = re.compile(r"[0-9a-f]{7,64}")
_DIFF_MAX_BYTES = 400_000


def _read_run_config(config_path: Path) -> dict[str, Any]:
    """Load display-only metadata from an explicitly supplied run config."""

    with config_path.open("rb") as stream:
        parsed = tomllib.load(stream)
    return parsed if isinstance(parsed, dict) else {}


def _nested(mapping: dict[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _execution_statuses(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Read the external worker lifecycle without exposing claim credentials."""

    raw_path = _nested(
        config, "environment", "settings", "action_runtime", "action_store_path"
    )
    if not isinstance(raw_path, str):
        return {}
    path = Path(raw_path).resolve()
    if not path.is_file():
        return {}
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT action_id, status, failure_code, claimed_at, completed_at "
                "FROM action_executions"
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.DatabaseError:
        return {}
    return {
        action_id: {
            "status": status,
            "failure_code": failure_code,
            "claimed_at": claimed_at,
            "completed_at": completed_at,
        }
        for action_id, status, failure_code, claimed_at, completed_at in rows
    }


def enrich_run_view(view: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    """Attach configured design facts and the live worker lifecycle to a view.

    These facts deliberately stay outside the canonical analysis builder: the
    WebUI may be given a config and an external action store, while an exported
    run remains valid and honest without either one.
    """

    settings = _nested(config, "environment", "settings")
    settings = settings if isinstance(settings, dict) else {}
    action_runtime = settings.get("action_runtime")
    action_runtime = action_runtime if isinstance(action_runtime, dict) else {}
    codex = action_runtime.get("codex")
    codex = codex if isinstance(codex, dict) else {}
    program = action_runtime.get("program")
    program = program if isinstance(program, dict) else {}
    evaluation = action_runtime.get("evaluation")
    evaluation = evaluation if isinstance(evaluation, dict) else {}
    sampling = evaluation.get("sampling")
    sampling = sampling if isinstance(sampling, dict) else {}
    budget = settings.get("budget")
    budget = budget if isinstance(budget, dict) else {}

    meta = view.setdefault("meta", {})
    worker = meta.setdefault("worker", {})
    provider = codex.get("provider")
    model = codex.get("model")
    if isinstance(provider, str):
        worker["provider"] = provider
    if isinstance(model, str):
        worker["model"] = model
    unavailable = worker.get("unavailable")
    if isinstance(unavailable, list):
        worker["unavailable"] = [
            field
            for field in unavailable
            if not (field == "provider" and isinstance(provider, str))
            and not (field == "model" and isinstance(model, str))
        ]
    solver_model = program.get("model_name")
    meta["solver"] = {
        "model": solver_model if isinstance(solver_model, str) else None,
        "source": "run config",
    }
    meta["configured_budget"] = {
        key: value
        for key, value in budget.items()
        if key in {"rounds", "directions"} and isinstance(value, (int, float))
    }

    units = sampling.get("units")
    population = len(units) if isinstance(units, list) else None
    protocol = view.setdefault("protocol", {})
    tiers = protocol.get("tiers")
    if isinstance(tiers, list):
        for tier in tiers:
            if not isinstance(tier, dict):
                continue
            if tier.get("tier") == "B" and population is not None:
                tier["cell_count"] = population
                tier["source"] = "configured evaluation population"
                if tier.get("state") is None:
                    tier["state"] = "configured"
            elif tier.get("tier") == "C" and tier.get("state") is None:
                tier["source"] = "not configured for this development run"
                tier["state"] = "not configured"
    design = protocol.get("estimator_design")
    design = dict(design) if isinstance(design, dict) else {}
    configured_design = {
        "population": population,
        "sample_size": sampling.get("size"),
        "replicates": sampling.get("replicates"),
        "plan_id": sampling.get("plan_id"),
        "scope_identity": sampling.get("scope_identity"),
        "selection_seed_base": sampling.get("selection_seed_base"),
        "information_weighting": sampling.get("information_weighting"),
    }
    for key, value in configured_design.items():
        if value is not None:
            design.setdefault(key, value)
    protocol["estimator_design"] = design

    statuses = _execution_statuses(config)
    for action in view.get("actions", ()):
        if not isinstance(action, dict):
            continue
        action_worker = action.get("worker")
        if isinstance(action_worker, dict):
            if isinstance(provider, str):
                action_worker["provider"] = provider
            if isinstance(model, str):
                action_worker["model"] = model
            action_unavailable = action_worker.get("unavailable")
            if isinstance(action_unavailable, list):
                action_worker["unavailable"] = [
                    field
                    for field in action_unavailable
                    if not (field == "provider" and isinstance(provider, str))
                    and not (field == "model" and isinstance(model, str))
                ]
        external = statuses.get(action.get("action_id"))
        if external is None:
            continue
        action["external_execution"] = external
        if action.get("lifecycle_stage") == "completed":
            continue
        status = external.get("status")
        if status == "blocked":
            action["lifecycle_stage"] = "blocked"
        elif status == "completed":
            action["lifecycle_stage"] = "evaluating"
        elif status == "claimed":
            action["lifecycle_stage"] = "running"

    warnings: list[str] = []
    configured_rounds = budget.get("rounds")
    expected_rounds = {
        batch.get("expected_rounds")
        for batch in view.get("batches", ())
        if isinstance(batch, dict)
    }
    if (
        isinstance(configured_rounds, int)
        and expected_rounds
        and expected_rounds != {configured_rounds}
    ):
        actual = ", ".join(str(value) for value in sorted(expected_rounds))
        warnings.append(
            f"The saved search budget says {configured_rounds} rounds, "
            f"but the launched batch planned {actual}."
        )
    meta["data_warnings"] = warnings

    # Warm-start programs are real searchable parents, but their originating
    # actions belong to another immutable run.  Keep that boundary explicit:
    # attach an external trace pointer for the inspector instead of copying the
    # source action/outcome into this run and making its local history untrue.
    raw_warm_starts = settings.get("warm_start_seeds")
    warm_starts = (
        tuple(item for item in raw_warm_starts if isinstance(item, dict))
        if isinstance(raw_warm_starts, list)
        else ()
    )
    programs_by_commit = {
        program.get("commit_sha"): program
        for program in view.get("programs", ())
        if isinstance(program, dict) and isinstance(program.get("commit_sha"), str)
    }
    for seed in warm_starts:
        commit_sha = seed.get("commit_sha")
        program = programs_by_commit.get(commit_sha)
        if program is None:
            continue
        source_run_id = seed.get("source_run_id")
        source_action_id = seed.get("source_action_id")
        archive_seq = seed.get("archive_seq")
        if not isinstance(source_run_id, str) or not isinstance(
            source_action_id, str
        ):
            continue
        program["warm_start"] = {
            "source_run_id": source_run_id,
            "source_action_id": source_action_id,
            "source_outcome_id": seed.get("source_outcome_id"),
            "admission_epoch": seed.get("admission_epoch"),
            "original_conservative_lower": seed.get(
                "original_conservative_lower"
            ),
            "warm_start_conservative_lower": seed.get(
                "warm_start_conservative_lower"
            ),
        }
        if isinstance(archive_seq, int) and not isinstance(archive_seq, bool):
            program["label"] = f"Warm-start program #{archive_seq}"
            program["archive_label"] = f"Warm start #{archive_seq}"
    return view


def attach_inherited_prehistory(
    view: dict[str, Any], source_views: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    """Project complete source attempts up to every accepted warm-start point.

    The source records remain owned by their immutable runs.  This projection
    gives the inspector a continuous visual history without rewriting their
    run ids, admission decisions, or persistence coordinates as local facts.
    """

    branches: list[dict[str, Any]] = []
    actions: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    planning_failures: list[dict[str, Any]] = []
    seen_actions: set[str] = set()
    seen_decisions: set[str] = set()
    seen_batches: set[tuple[str, str]] = set()
    seen_failures: set[tuple[str, str, int]] = set()

    for program in view.get("programs", ()):
        if not isinstance(program, dict):
            continue
        warm = program.get("warm_start")
        if not isinstance(warm, dict):
            continue
        source_run_id = warm.get("source_run_id")
        target_action_id = warm.get("source_action_id")
        source = source_views.get(source_run_id)
        if (
            not isinstance(source_run_id, str)
            or not isinstance(target_action_id, str)
            or not isinstance(source, dict)
        ):
            continue
        source_actions = tuple(
            item for item in source.get("actions", ()) if isinstance(item, dict)
        )
        target = next(
            (
                item
                for item in source_actions
                if item.get("action_id") == target_action_id
            ),
            None,
        )
        if target is None:
            continue
        target_ordinal = target.get("ordinal")
        if not isinstance(target_ordinal, int) or isinstance(target_ordinal, bool):
            continue
        selected = tuple(
            item
            for item in source_actions
            if isinstance(item.get("ordinal"), int)
            and not isinstance(item.get("ordinal"), bool)
            and item["ordinal"] <= target_ordinal
        )
        selected_ids = {
            item["action_id"]
            for item in selected
            if isinstance(item.get("action_id"), str)
        }
        selected_batch_ids = {
            item["batch_id"]
            for item in selected
            if isinstance(item.get("batch_id"), str)
        }
        for source_action in selected:
            action_id = source_action.get("action_id")
            if not isinstance(action_id, str) or action_id in seen_actions:
                continue
            inherited = deepcopy(source_action)
            inherited["prehistory"] = {
                "source_run_id": source_run_id,
                "warm_start_target": action_id == target_action_id,
                "accepted_commit_sha": program.get("commit_sha"),
            }
            actions.append(inherited)
            seen_actions.add(action_id)
        for source_decision in source.get("decisions", ()):
            if not isinstance(source_decision, dict):
                continue
            action_id = source_decision.get("action_id")
            if action_id not in selected_ids or action_id in seen_decisions:
                continue
            decisions.append(deepcopy(source_decision))
            seen_decisions.add(action_id)
        for source_batch in source.get("batches", ()):
            if not isinstance(source_batch, dict):
                continue
            batch_id = source_batch.get("batch_id")
            key = (source_run_id, batch_id)
            if batch_id not in selected_batch_ids or key in seen_batches:
                continue
            inherited_batch = deepcopy(source_batch)
            inherited_batch["source_run_id"] = source_run_id
            batches.append(inherited_batch)
            seen_batches.add(key)
        for failure in source.get("planning_failures", ()):
            if not isinstance(failure, dict):
                continue
            batch_id = failure.get("batch_id")
            round_index = failure.get("round_index")
            key = (source_run_id, batch_id, round_index)
            if batch_id not in selected_batch_ids or key in seen_failures:
                continue
            inherited_failure = deepcopy(failure)
            inherited_failure["source_run_id"] = source_run_id
            planning_failures.append(inherited_failure)
            seen_failures.add(key)
        branches.append(
            {
                "source_run_id": source_run_id,
                "target_action_id": target_action_id,
                "accepted_commit_sha": program.get("commit_sha"),
                "archive_seq": program.get("archive_seq"),
                "source_action_count": len(selected),
            }
        )

    view["prehistory"] = {
        "branches": branches,
        "actions": actions,
        "decisions": decisions,
        "batches": batches,
        "planning_failures": planning_failures,
    }
    return view


def list_run_ids(database_path: Path | str) -> tuple[str, ...]:
    """Read the distinct run ids out of the persisted batch envelopes."""

    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT payload FROM records WHERE bucket = 'batches'"
        ).fetchall()
    except sqlite3.DatabaseError:
        return ()
    finally:
        connection.close()
    found: set[str] = set()
    for (payload,) in rows:
        try:
            envelope = json.loads(payload)
        except json.JSONDecodeError:
            continue
        for name, value in envelope.get("fields", ()):
            if name == "run_id" and isinstance(value, str):
                found.add(value)
    return tuple(sorted(found))


def commit_diff(repositories: tuple[Path, ...], sha: str) -> dict[str, Any]:
    """Read one candidate commit out of the repository the run evolved.

    The patch is the only view of what a candidate actually wrote, so it is
    served from the repository rather than reconstructed from run records.  Runs
    carried by different policies keep separate checkouts, so each is tried in
    turn; a run whose commits are not real git objects reports that plainly
    instead of erroring.
    """

    if not repositories:
        return {"sha": sha, "unavailable": "no repository was configured for this server"}
    if not _SHA_RE.fullmatch(sha):
        return {"sha": sha, "unavailable": "this run does not record real git commits"}
    detail = ""
    for repository in repositories:
        process = subprocess.run(
            ["git", "-C", str(repository), "show", "--no-color", "--stat", "--patch", sha],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if process.returncode == 0:
            raw = process.stdout
            subject_process = subprocess.run(
                ["git", "-C", str(repository), "show", "-s", "--format=%s", sha],
                capture_output=True,
                timeout=30,
                check=False,
            )
            subject = (
                subject_process.stdout.decode(errors="replace").strip()
                if subject_process.returncode == 0
                else None
            )
            return {
                "sha": sha,
                "subject": subject or None,
                "text": raw[:_DIFF_MAX_BYTES].decode(errors="replace"),
                "truncated": len(raw) > _DIFF_MAX_BYTES,
            }
        lines = process.stderr.decode(errors="replace").strip().splitlines()
        detail = detail or (lines[0][:200] if lines else "")
    return {"sha": sha, "unavailable": detail or "commit not found in any repository"}


class _Handler(BaseHTTPRequestHandler):
    database_paths: tuple[Path, ...]
    research_root: Path | None = None
    repositories: tuple[Path, ...] = ()
    configurations: dict[Path, dict[str, Any]] = {}
    server_version = "agentpilot-inspector"

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path in {"", "/"}:
            self._send_static("index.html")
            return
        if path == "/api/runs":
            self._send_json({"runs": list(self._catalogue())})
            return
        if path == '/api/research' and self.research_root is not None:
            from .research import snapshot
            self._send_json(snapshot(self.research_root))
            return
        if path == "/api/diff":
            sha = parse_qs(parsed.query).get("sha", [""])[0]
            if not sha:
                self._send_json({"error": "sha is required"}, status=400)
                return
            repositories = self.repositories
            if self.research_root is not None:
                from .research_view import catalogue, repository
                run_id = parse_qs(parsed.query).get('run', [''])[0]
                entry = catalogue(self.research_root).get(run_id)
                view = self._run_view(run_id) if entry else {}
                recorded = {p.get('commit_sha') for p in view.get('programs', [])}
                recorded.update(a['execution'].get('result_commit_sha') for a in view.get('actions', []))
                if sha not in recorded:
                    self._send_json({'sha':sha, 'unavailable':'Commit is not recorded in this research run'})
                    return
                repo = repository(entry)
                repositories = (repo,) if repo else ()
            self._send_json(commit_diff(repositories, sha))
            return
        if path.startswith("/api/run/"):
            run_id = path[len("/api/run/") :]
            if not run_id:
                self._send_json({"error": "run id is required"}, status=400)
                return
            self._send_json(self._run_view(run_id))
            return
        self._send_static(path.lstrip("/"))

    def _catalogue(self) -> dict[str, Path]:
        """Map every run id served here back to the database that holds it."""

        found: dict[str, Path] = {}
        if self.research_root is not None:
            from .research_view import catalogue
            found.update({key:self.research_root/entry['output']/'portfolio.sqlite3'
                          for key,entry in catalogue(self.research_root).items()})
        for database in self.database_paths:
            for run_id in list_run_ids(database):
                found.setdefault(run_id, database)
        return dict(sorted(found.items()))

    def _run_view(self, run_id: str) -> dict[str, Any]:
        if self.research_root is not None:
            from .research_view import catalogue, build_view
            if run_id in catalogue(self.research_root):
                return build_view(self.research_root, run_id)
        database = self._catalogue().get(run_id)
        if database is None:
            return {"error": f"unknown run: {run_id}"}
        adapter = RunViewAdapter(SQLiteReadOnlyUnitOfWorkFactory(database))
        view = adapter.build_detail(run_id)
        config = self.configurations.get(database)
        if config is not None:
            enrich_run_view(view, config)
        source_run_ids = {
            warm.get("source_run_id")
            for program in view.get("programs", ())
            if isinstance(program, dict)
            and isinstance((warm := program.get("warm_start")), dict)
            and isinstance(warm.get("source_run_id"), str)
        }
        if source_run_ids:
            catalogue = self._catalogue()
            source_views: dict[str, dict[str, Any]] = {}
            for source_run_id in sorted(source_run_ids):
                source_database = catalogue.get(source_run_id)
                if source_database is None:
                    continue
                source_adapter = RunViewAdapter(
                    SQLiteReadOnlyUnitOfWorkFactory(source_database)
                )
                source_view = source_adapter.build_detail(source_run_id)
                source_config = self.configurations.get(source_database)
                if source_config is not None:
                    enrich_run_view(source_view, source_config)
                source_views[source_run_id] = source_view
            attach_inherited_prehistory(view, source_views)
        if "-aborted-" in database.parent.name:
            meta = view.setdefault("meta", {})
            meta["run_storage_state"] = "aborted archive"
            warnings = meta.setdefault("data_warnings", [])
            warnings.insert(
                0,
                "This is a read-only snapshot of an experiment archived after it stopped.",
            )
            for action in view.get("actions", ()):
                if action.get("lifecycle_stage") in {
                    "planned",
                    "running",
                    "evaluating",
                }:
                    action["lifecycle_stage"] = "aborted"
        return view

    def _send_static(self, relative: str) -> None:
        target = (_STATIC_ROOT / relative).resolve()
        if not target.is_file() or _STATIC_ROOT not in target.parents:
            self.send_error(404)
            return
        body = target.read_bytes()
        self.send_response(200)
        self.send_header(
            "Content-Type",
            _CONTENT_TYPES.get(target.suffix, "application/octet-stream"),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], *, status: int = 200) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        return


def serve(
    *database_paths: Path | str,
    repositories: Sequence[Path | str] = (),
    config_paths: Sequence[Path | str] = (),
    research_root: Path | str | None = None,
    host: str = "127.0.0.1",
    port: int = 8765,
):
    """Build a loopback server that projects the given databases read-only.

    Several databases can be served at once so runs carried by different
    policies sit side by side in one run picker.  ``repositories`` are the
    checkouts the candidates committed into; without them the inspector simply
    reports that patches are unavailable.
    """

    if not database_paths and research_root is None:
        raise ValueError("at least one database is required")
    resolved: list[Path] = []
    for candidate in database_paths:
        path = Path(candidate).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"database not found: {path}")
        resolved.append(path)
    repos: list[Path] = []
    for candidate in repositories:
        repo = Path(candidate).resolve()
        if not (repo / ".git").exists():
            raise FileNotFoundError(f"not a git repository: {repo}")
        repos.append(repo)
    configurations: dict[Path, dict[str, Any]] = {}
    for candidate in config_paths:
        config_path = Path(candidate).resolve()
        if not config_path.is_file():
            raise FileNotFoundError(f"config not found: {config_path}")
        config = _read_run_config(config_path)
        configured_database = _nested(config, "runtime", "database_path")
        if not isinstance(configured_database, str):
            raise ValueError(f"config has no runtime.database_path: {config_path}")
        configured_path = Path(configured_database).resolve()
        matches = [
            database
            for database in resolved
            if database == configured_path or database.name == configured_path.name
        ]
        if len(matches) != 1:
            raise ValueError(
                f"config database is not served here: {configured_database}"
            )
        database = matches[0]
        if database != configured_path:
            action_runtime = _nested(
                config, "environment", "settings", "action_runtime"
            )
            if isinstance(action_runtime, dict):
                action_store = action_runtime.get("action_store_path")
                if isinstance(action_store, str):
                    archived_store = database.parent / Path(action_store).name
                    if archived_store.is_file():
                        action_runtime["action_store_path"] = str(archived_store)
        configurations[database] = config
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {
            "database_paths": tuple(resolved),
            "repositories": tuple(repos),
            "configurations": configurations,
            "research_root": Path(research_root).resolve() if research_root is not None else None,
        },
    )
    return ThreadingHTTPServer((host, port), handler)


__all__ = [
    "attach_inherited_prehistory",
    "commit_diff",
    "enrich_run_view",
    "list_run_ids",
    "serve",
]
