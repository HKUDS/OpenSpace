"""OpenSpace MCP Server

Exposes the following tools to MCP clients:
  cloud_auth_flow — Register/login and provision cloud agent API keys
  execute_task   — Delegate a task (auto-registers skills, auto-searches, auto-evolves)
  search_skills  — Standalone local skill search
  cloud_browse_skills — LLM-guided cloud package/skill browsing and import
  fix_skill      — Run a manual FIX job for a broken skill through evolution
  upload_skill   — Upload a local skill to cloud after resolving placement/metadata

Usage:
    python -m openspace.entrypoints.mcp.server                     # auto (TTY -> SSE, MCP host -> stdio)
    python -m openspace.entrypoints.mcp.server --transport sse     # SSE on port 8080
    python -m openspace.entrypoints.mcp.server --transport streamable-http  # Streamable HTTP on port 8081
    python -m openspace.entrypoints.mcp.server --port 9090         # SSE on custom port

Environment variables: see ``openspace/host_detection/`` and ``openspace/cloud/auth_flow.py``.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


class _MCPSafeStdout:
    """Stdout wrapper: binary (.buffer) → real stdout, text (.write) → stderr."""

    def __init__(self, real_stdout, stderr):
        self._real = real_stdout
        self._stderr = stderr

    @property
    def buffer(self):
        return self._real.buffer

    def fileno(self):
        return self._real.fileno()

    def write(self, s):
        return self._stderr.write(s)

    def writelines(self, lines):
        return self._stderr.writelines(lines)

    def flush(self):
        self._stderr.flush()
        self._real.flush()

    def isatty(self):
        return self._stderr.isatty()

    @property
    def encoding(self):
        return self._stderr.encoding

    @property
    def errors(self):
        return self._stderr.errors

    @property
    def closed(self):
        return self._stderr.closed

    def readable(self):
        return False

    def writable(self):
        return True

    def seekable(self):
        return False

    def __getattr__(self, name):
        return getattr(self._stderr, name)

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "logs"
_LOG_DIR.mkdir(parents=True, exist_ok=True)

_real_stdout = sys.stdout

# Windows pipe buffers are small. When using stdio MCP transport,
# the parent process only reads stdout for MCP messages and does NOT
# drain stderr. Heavy log/print output during execute_task fills the stderr
# pipe buffer, blocking this process on write() → deadlock → timeout.
# Redirect stderr to a log file on Windows to prevent this.
if os.name == "nt":
    _stderr_file = open(
        _LOG_DIR / "mcp_stderr.log", "a", encoding="utf-8", buffering=1
    )
    sys.stderr = _stderr_file

sys.stdout = _MCPSafeStdout(_real_stdout, sys.stderr)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.FileHandler(_LOG_DIR / "mcp_server.log")],
)
logger = logging.getLogger(__name__)

from openspace.entrypoints.mcp.response import (
    format_task_result as _format_task_result,
    json_error as _json_error,
    json_ok as _json_ok,
)
from openspace.runtime import ExecutionRequest

from mcp.server.fastmcp import FastMCP

_fastmcp_kwargs: dict = {}
try:
    if "description" in inspect.signature(FastMCP.__init__).parameters:
        _fastmcp_kwargs["description"] = (
            "OpenSpace: Unite the Agents. Evolve the Mind. Rebuild the World."
        )
except (TypeError, ValueError):
    pass

mcp = FastMCP("OpenSpace", **_fastmcp_kwargs)

_openspace_instance = None
_openspace_lock = asyncio.Lock()

# Internal state: tracks bot skill directories already registered this session.
_registered_skill_dirs: set = set()

_UPLOAD_META_FILENAME = ".upload_meta.json"


def _resolve_session_storage_dir(workspace: str | None) -> str | None:
    """Keep MCP runtime session files inside an explicit or workspace-local root."""

    explicit = os.environ.get("OPENSPACE_SESSION_STORAGE_DIR")
    if explicit:
        return str(Path(explicit).expanduser().resolve())

    config_home = os.environ.get("OPENSPACE_CONFIG_HOME")
    if config_home:
        return str(Path(config_home).expanduser().resolve())

    if workspace:
        return str((Path(workspace).expanduser().resolve() / ".openspace"))

    return None


async def _get_openspace():
    """Lazy-initialise the OpenSpace engine."""
    global _openspace_instance
    if _openspace_instance is not None and _openspace_instance.is_initialized():
        return _openspace_instance

    async with _openspace_lock:
        if _openspace_instance is not None and _openspace_instance.is_initialized():
            return _openspace_instance

        logger.info("Initializing OpenSpace engine ...")
        from openspace import OpenSpace, OpenSpaceConfig
        from openspace.host_detection import (
            build_grounding_config_path,
            build_llm_kwargs,
            load_runtime_env,
        )

        load_runtime_env()

        env_model = os.environ.get("OPENSPACE_MODEL", "")
        workspace = os.environ.get("OPENSPACE_WORKSPACE")
        max_iter = int(os.environ.get("OPENSPACE_MAX_ITERATIONS", "20"))
        enable_rec = os.environ.get("OPENSPACE_ENABLE_RECORDING", "true").lower() in ("true", "1", "yes")

        backend_scope_raw = os.environ.get("OPENSPACE_BACKEND_SCOPE")
        backend_scope = (
            [b.strip() for b in backend_scope_raw.split(",") if b.strip()]
            if backend_scope_raw else None
        )

        config_path = build_grounding_config_path()
        model, llm_kwargs = build_llm_kwargs(env_model)

        _pkg_root = str(_PROJECT_ROOT)
        recording_base = workspace or _pkg_root
        recording_log_dir = str(Path(recording_base) / "logs" / "recordings")

        config = OpenSpaceConfig(
            llm_model=model,
            llm_kwargs=llm_kwargs,
            workspace_dir=workspace,
            session_storage_dir=_resolve_session_storage_dir(workspace),
            grounding_max_iterations=max_iter,
            enable_recording=enable_rec,
            recording_backends=["shell"] if enable_rec else None, # ["shell", "mcp", "web"] if enable_rec else None
            recording_log_dir=recording_log_dir,
            backend_scope=backend_scope,
            grounding_config_path=config_path,
        )

        _openspace_instance = OpenSpace(config=config)
        await _openspace_instance.initialize()
        logger.info("OpenSpace engine ready (model=%s).", model)

        # Auto-register host bot skill directories from env (set once by human)
        host_skill_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
        if host_skill_dirs_raw:
            dirs = [d.strip() for d in host_skill_dirs_raw.split(",") if d.strip()]
            if dirs:
                await _auto_register_skill_dirs(dirs)
                logger.info("Auto-registered host skill dirs from OPENSPACE_HOST_SKILL_DIRS: %s", dirs)

        return _openspace_instance


async def _get_runtime_store(*, required: bool = True):
    """Return the SkillStore owned by the OpenSpace runtime."""

    openspace = await _get_openspace()
    store = openspace.get_skill_store()
    if store and not getattr(store, "_closed", False):
        return store
    if required:
        raise RuntimeError("SkillStore is not initialized")
    return None


async def _get_local_search_context():
    """Return local skills without booting the full agent runtime for search.

    ``search_skills`` is a discovery-only MCP tool. Initializing OpenSpace here
    also initializes grounding providers, memory, evolution, and model config,
    which can take longer than a normal MCP tool timeout on Windows. Reuse the
    runtime registry when it already exists; otherwise build the same canonical
    registry directly and omit optional quality-store enrichment.
    """

    if _openspace_instance is not None and _openspace_instance.is_initialized():
        host_skill_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
        if host_skill_dirs_raw:
            env_dirs = [
                directory.strip()
                for directory in host_skill_dirs_raw.split(",")
                if directory.strip()
            ]
            if env_dirs:
                await _auto_register_skill_dirs(env_dirs)

        registry = _openspace_instance.get_skill_registry()
        if not registry:
            return None, None
        store = _openspace_instance.get_skill_store()
        if store and getattr(store, "_closed", False):
            store = None
        return registry.list_skills(), store

    from openspace.runtime.skill_registry import build_skill_registry

    registry = await asyncio.to_thread(
        build_skill_registry,
        workspace_dir=os.environ.get("OPENSPACE_WORKSPACE"),
    )
    if not registry:
        return None, None
    return registry.list_skills(), None


async def _get_cloud_mapping_store():
    from openspace.cloud.local_mapping import CloudLocalMappingStore

    db_path = None
    if _openspace_instance is not None:
        try:
            store = _openspace_instance.get_skill_store()
            if store and not getattr(store, "_closed", False):
                db_path = getattr(store, "db_path", None)
        except Exception as exc:
            logger.debug(f"Cloud mapping store DB path lookup failed: {exc}")
    return CloudLocalMappingStore(db_path)


def _get_cloud_client(*, mapping_store=None):
    """Get a OpenSpaceClient instance (raises CloudError if not configured)."""
    from openspace.cloud.client import OpenSpaceClient
    from openspace.cloud.config import load_cloud_config

    return OpenSpaceClient(load_cloud_config(), mapping_store=mapping_store)


def _normalize_upload_origin(origin: str) -> str:
    mapping = {
        "imported": "imported",
        "captured": "capture",
        "capture": "capture",
        "derived": "derive",
        "derive": "derive",
        "fixed": "fix",
        "fix": "fix",
    }
    normalized = mapping.get(origin)
    if not normalized:
        raise ValueError("origin must be imported, captured/capture, derived/derive, or fixed/fix")
    return normalized


def _cloud_available_for_implicit_use() -> bool:
    """Return whether automatic cloud discovery should run."""

    try:
        from openspace.cloud.config import load_cloud_config

        config = load_cloud_config()
    except Exception as exc:
        logger.warning("Cloud config invalid; skipping automatic cloud use: %s", exc)
        return False
    if not config.enabled:
        return False
    if not config.api_key:
        logger.warning("OPENSPACE_CLOUD_API_KEY is required; skipping automatic cloud use")
        return False
    return True


def _write_upload_meta(skill_dir: Path, info: Dict[str, Any]) -> None:
    """Write ``.upload_meta.json`` so ``upload_skill`` can read pre-saved metadata.

    Called after a validated evolution action commits.
    The upload tool still requires package placement for non-fix v2 uploads
    unless placement has already been saved in this metadata.
    """
    meta = {
        "origin": info.get("origin", "imported"),
        "parent_local_skill_ids": info.get("parent_local_skill_ids", []),
        "parent_cloud_skill_ids": info.get("parent_cloud_skill_ids", []),
        "change_summary": info.get("change_summary", ""),
        "created_by": info.get("created_by", "openspace"),
        "tags": info.get("tags", []),
    }
    if isinstance(info.get("upload_placement"), dict):
        meta["upload_placement"] = info["upload_placement"]
    meta_path = skill_dir / _UPLOAD_META_FILENAME
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        logger.debug(f"Wrote upload metadata to {meta_path}")
    except Exception as e:
        logger.warning(f"Failed to write upload metadata: {e}")


def _save_upload_meta(skill_dir: Path, meta: Dict[str, Any]) -> None:
    meta_path = skill_dir / _UPLOAD_META_FILENAME
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


async def _read_upload_meta(skill_dir: Path) -> Dict[str, Any]:
    """Read upload metadata with three-tier fallback.

    Resolution order:
      1. ``.upload_meta.json`` sidecar file (written right after evolution)
      2. SkillStore DB lookup by path (long-term persistence)
      3. Empty dict (caller applies defaults)

    This ensures metadata survives even if the sidecar file is deleted
    or the user comes back to upload much later.
    """
    # Tier 1: sidecar file
    meta_path = skill_dir / _UPLOAD_META_FILENAME
    if meta_path.exists():
        try:
            data = json.loads(meta_path.read_text(encoding="utf-8"))
            if data:
                return data
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Failed to read upload metadata file: {e}")

    # Tier 2: DB lookup
    try:
        store = await _get_runtime_store(required=False)
        if store:
            rec = store.load_record_by_path(str(skill_dir))
            if rec:
                logger.debug(f"Upload metadata resolved from DB for {skill_dir}")
                return {
                    "origin": rec.lineage.origin.value,
                    "parent_local_skill_ids": rec.lineage.parent_skill_ids,
                    "parent_cloud_skill_ids": [],
                    "change_summary": rec.lineage.change_summary,
                    "created_by": rec.lineage.created_by or "",
                    "tags": rec.tags,
                }
    except Exception as e:
        logger.debug(f"DB upload metadata lookup failed: {e}")

    return {}


async def _auto_register_skill_dirs(skill_dirs: List[str]) -> int:
    """Register bot skill directories into OpenSpace's SkillRegistry + DB.

    Called automatically by ``execute_task`` on every invocation. Directories
    are re-scanned each time so that skills created by the host bot since the last call are discovered immediately.
    """
    global _registered_skill_dirs

    valid_dirs = [Path(d) for d in skill_dirs if Path(d).is_dir()]
    if not valid_dirs:
        return 0

    openspace = await _get_openspace()
    _register_evidence_read_roots(openspace, *valid_dirs)
    registry = openspace.get_skill_registry()
    if not registry:
        logger.warning("_auto_register_skill_dirs: SkillRegistry not initialized")
        return 0

    added = registry.discover_from_dirs(valid_dirs)

    db_created = 0
    if added:
        store = await _get_runtime_store(required=False)
        if store:
            db_created = await store.sync_from_registry(added)

    is_first = any(d not in _registered_skill_dirs for d in skill_dirs)
    for d in skill_dirs:
        _registered_skill_dirs.add(d)

    if added:
        action = "Auto-registered" if is_first else "Re-scanned & found"
        logger.info(
            f"{action} {len(added)} skill(s) from {len(valid_dirs)} dir(s), "
            f"{db_created} new DB record(s)"
        )
    return len(added)


def _register_evidence_read_roots(openspace: Any, *roots: Any) -> None:
    runtime = getattr(openspace, "runtime", None)
    register = getattr(runtime, "register_evidence_read_roots", None)
    if not callable(register):
        return
    try:
        register(*roots)
    except Exception:
        logger.debug("MCP evidence read root registration skipped", exc_info=True)


async def _cloud_search_candidates(task: str, limit: int = 8) -> List[Dict[str, Any]]:
    """Search cloud for skills relevant to *task* and return import candidates.

    This is intentionally discovery-only. Cloud skills must not be downloaded
    silently because the agent needs to inspect the local package taxonomy and
    choose a ``local_category_path`` before import.
    """
    try:
        normalized_task_query = task.strip()
        if not normalized_task_query:
            return []

        cloud_client = _get_cloud_client()
        cloud_search_results = await asyncio.to_thread(
            cloud_client.search_cloud_skills,
            query=normalized_task_query,
            limit=min(limit * 2, 50),
        )
        if not cloud_search_results:
            return []

        cloud_hits = [
            cloud_result for cloud_result in cloud_search_results
            if cloud_result.get("cloud_skill_id")
        ][:limit]

        import_results: List[Dict[str, Any]] = []
        for cloud_hit in cloud_hits:
            cloud_skill_id = str(cloud_hit.get("cloud_skill_id") or "")
            if not cloud_skill_id:
                continue
            import_results.append({
                "cloud_skill_id": cloud_skill_id,
                "name": cloud_hit.get("name", ""),
                "summary": cloud_hit.get("description") or cloud_hit.get("summary", ""),
                "package_id": cloud_hit.get("package_id", ""),
                "package_path": cloud_hit.get("package_path", ""),
                "import_status": "needs_local_category_path",
                "required_next_tool": "cloud_browse_skills",
                "required_next_action": "local_taxonomy_then_import_skill",
            })

        if import_results:
            logger.info(
                "Cloud search found %d skill candidate(s); explicit local taxonomy "
                "selection is required before import",
                len(import_results),
            )
        return import_results

    except Exception as e:
        logger.warning(f"_cloud_search_candidates failed (non-fatal): {e}")
        return []


async def _do_import_cloud_skill(
    cloud_skill_id: str,
    target_dir: Optional[str] = None,
    *,
    local_category: str | None = None,
    local_category_path: str | None = None,
) -> Dict[str, Any]:
    """Download a cloud skill and register it locally."""
    client = _get_cloud_client()

    if target_dir:
        base_dir = Path(target_dir)
    else:
        host_ws = (
            os.environ.get("NANOBOT_WORKSPACE")
            or os.environ.get("OPENCLAW_STATE_DIR")
        )
        if host_ws:
            base_dir = Path(host_ws) / "skills"
            base_dir.mkdir(parents=True, exist_ok=True)
        else:
            openspace = await _get_openspace()
            grounding_config = openspace.get_grounding_config()
            skill_cfg = grounding_config.skills if grounding_config else None
            if skill_cfg and skill_cfg.skill_dirs:
                base_dir = Path(skill_cfg.skill_dirs[0])
            else:
                base_dir = _PACKAGE_ROOT / "skills"

    result = await asyncio.to_thread(
        client.import_skill,
        cloud_skill_id,
        base_dir,
        local_category=local_category,
        local_category_path=local_category_path,
    )

    skill_dir = Path(result.get("local_path", ""))
    if skill_dir.exists():
        openspace = await _get_openspace()
        _register_evidence_read_roots(openspace, skill_dir)
        registry = openspace.get_skill_registry()
        if registry:
            meta = registry.register_skill_dir(skill_dir)
            if meta:
                store = await _get_runtime_store(required=False)
                if store:
                    await store.sync_from_registry([meta])
                _register_evidence_read_roots(openspace, skill_dir)
                result["registered"] = True

    result.setdefault("registered", False)
    return result


def _resolve_cloud_import_base_dir(target_dir: Optional[str] = None) -> Path:
    """Resolve a writable local root for cloud package imports."""

    if target_dir:
        base_dir = Path(target_dir)
    else:
        host_ws = (
            os.environ.get("NANOBOT_WORKSPACE")
            or os.environ.get("OPENCLAW_STATE_DIR")
            or os.environ.get("OPENSPACE_WORKSPACE")
        )
        base_dir = Path(host_ws) / "skills" if host_ws else _PACKAGE_ROOT / "skills"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def _summarize_recall_package(package: Dict[str, Any]) -> Dict[str, Any]:
    previews = [
        {
            "cloud_skill_id": str(preview.get("cloud_skill_id") or ""),
            "skill_name": str(preview.get("skill_name") or preview.get("title") or ""),
            "preview_text": str(preview.get("preview_text") or preview.get("summary") or ""),
        }
        for preview in (package.get("preview_entries") or [])
        if isinstance(preview, dict)
    ]
    return {
        "package_id": str(package.get("package_id") or ""),
        "package_path": str(package.get("package_path") or ""),
        "package_kind": str(package.get("package_kind") or ""),
        "package_display_name": str(package.get("package_display_name") or ""),
        "summary_line": str(package.get("summary_line") or ""),
        "display_scope": str(package.get("display_scope") or ""),
        "rank": package.get("rank"),
        "score": package.get("score"),
        "preview_entries": previews,
    }


def _summarize_cloud_skill_candidate(skill: Dict[str, Any]) -> Dict[str, Any]:
    metadata = skill.get("authored_metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    return {
        "cloud_skill_id": str(skill.get("cloud_skill_id") or skill.get("skill_id") or ""),
        "title": str(skill.get("title") or metadata.get("name") or skill.get("skill_name") or ""),
        "summary": str(skill.get("summary") or metadata.get("description") or skill.get("snippet") or ""),
        "package_id": str(skill.get("package_id") or ""),
        "package_path": str(skill.get("package_path") or ""),
        "effective_visibility": str(skill.get("effective_visibility") or skill.get("access_mode") or ""),
        "manifest_hash": skill.get("manifest_hash"),
        "artifact_dir": skill.get("artifact_dir"),
        "artifact_state": skill.get("artifact_state"),
        "downloadable": skill.get("downloadable"),
        "metadata_only": skill.get("metadata_only"),
        "rank": skill.get("rank"),
        "score": skill.get("score"),
        "match_mode": skill.get("match_mode"),
        "snippet": skill.get("snippet"),
    }


def _summarize_projection_pull(
    pull: Dict[str, Any],
    *,
    max_packages: int,
    max_skills: int,
) -> Dict[str, Any]:
    all_packages = pull.get("packages") or []
    all_skills = pull.get("skills") or []
    packages = [
        {
            "package_id": str(package.get("package_id") or ""),
            "package_path": str(package.get("package_path") or ""),
            "package_kind": str(package.get("package_kind") or ""),
            "package_display_name": str(package.get("package_display_name") or ""),
            "parent_package_id": package.get("parent_package_id"),
            "outline_path": package.get("outline_path"),
        }
        for package in all_packages[:max_packages]
        if isinstance(package, dict)
    ]
    skills = [
        _summarize_cloud_skill_candidate(skill)
        for skill in all_skills[:max_skills]
        if isinstance(skill, dict)
    ]
    return {
        "root_package_id": str(pull.get("root_package_id") or ""),
        "root_package_path": str(pull.get("root_package_path") or ""),
        "bundle_version": pull.get("bundle_version"),
        "projection_hash": pull.get("projection_hash"),
        "serving_epoch": pull.get("serving_epoch"),
        "source_epoch": pull.get("source_epoch"),
        "packages": packages,
        "skills": skills,
        "package_count": len(all_packages),
        "skill_count": len(all_skills),
        "packages_truncated": len(all_packages) > len(packages),
        "skills_truncated": len(all_skills) > len(skills),
    }


# MCP Tools
@mcp.tool()
async def cloud_auth_flow(
    action: str,
    email: str | None = None,
    password: str | None = None,
    name: str | None = None,
    agent_name: str = "openspace-local-agent",
    agent_id: str | None = None,
    persist: bool = True,
    credentials_path: str | None = None,
) -> str:
    """Register/login users and provision cloud agent API keys.

    Supported actions:
      - register_user
      - login_user
      - bootstrap_agent_key
      - list_agents
      - rotate_agent_key
      - verify_agent_key

    This tool never returns raw API keys, bearer tokens, or passwords.  Agent
    keys created by bootstrap/rotate are saved to local OPENSPACE_CLOUD_* config
    by default.  ``agent_name`` is owner-scoped: recovery only searches the
    currently authenticated user's agents.
    """

    try:
        from openspace.cloud.auth_flow import cloud_auth_flow as run_cloud_auth_flow

        result = await asyncio.to_thread(
            run_cloud_auth_flow,
            action=action,
            email=email,
            password=password,
            name=name,
            agent_name=agent_name,
            agent_id=agent_id,
            persist=persist,
            credentials_path=credentials_path,
        )
        return _json_ok(result)
    except Exception as e:
        from openspace.cloud.redaction import redact_cloud_secret

        logger.error("cloud_auth_flow failed: %s", redact_cloud_secret(str(e)), exc_info=True)
        return _json_error(redact_cloud_secret(str(e)), status="error")


@mcp.tool()
async def execute_task(
    task: str,
    workspace_dir: str | None = None,
    max_iterations: int | None = None,
    skill_dirs: list[str] | None = None,
    search_scope: str = "all",
) -> str:
    """Execute a task with OpenSpace's full grounding engine.

    OpenSpace will:
    1. Auto-register bot skills from skill_dirs (if provided)
    2. Search for relevant skills (scope controls local vs cloud+local)
    3. Attempt skill-guided execution → fallback to pure tools
    4. Auto-analyze → auto-evolve (FIX/DERIVED/CAPTURED) if needed

    If skills are auto-evolved, the response includes ``evolved_skills``
    with ``upload_ready: true``.  For non-fix uploads without pre-saved
    placement, call ``upload_skill`` with ``skill_dir``, ``visibility``, and
    ``cloud_package_path``.

    Note: This call blocks until the task completes (may take minutes).
    Set MCP client tool-call timeout ≥ 600 seconds.

    Args:
        task: The task instruction (natural language).
        workspace_dir: Working directory. Defaults to OPENSPACE_WORKSPACE env.
        max_iterations: Max agent iterations (default: 20).
        skill_dirs: Bot's skill directories to auto-register so OpenSpace
                    can select and track them.  Directories are re-scanned
                    on every call to discover skills created since the last
                    invocation.
        search_scope: Skill search scope before execution.
                      "all" (default) — local + cloud; falls back to local
                      if no API key is configured.
                      "local" — local SkillRegistry only (fast, no cloud).
    """
    try:
        openspace = await _get_openspace()

        # Re-scan host skill directories (from env) to pick up skills
        # created by the host bot since the last call.
        host_skill_dirs_raw = os.environ.get("OPENSPACE_HOST_SKILL_DIRS", "")
        if host_skill_dirs_raw:
            env_dirs = [d.strip() for d in host_skill_dirs_raw.split(",") if d.strip()]
            if env_dirs:
                await _auto_register_skill_dirs(env_dirs)

        # Auto-register bot skill directories (from call parameter)
        if skill_dirs:
            await _auto_register_skill_dirs(skill_dirs)

        # Determine where CAPTURED skills should be written.
        # Prefer the explicit skill_dirs parameter (= calling host agent's dir),
        # then fall back to the first env-based host skill dir.
        capture_skill_dir: str | None = None
        if skill_dirs:
            capture_skill_dir = skill_dirs[0]
        elif host_skill_dirs_raw:
            first_env = next(
                (d.strip() for d in host_skill_dirs_raw.split(",") if d.strip()),
                None,
            )
            if first_env:
                capture_skill_dir = first_env

        # Cloud search + import (if requested)
        cloud_skill_candidates: List[Dict[str, Any]] = []
        if search_scope == "all" and _cloud_available_for_implicit_use():
            cloud_skill_candidates = await _cloud_search_candidates(task)

        # Execute
        result = await openspace.execute(
            ExecutionRequest(
                prompt=task,
                workspace_dir=workspace_dir,
                max_iterations=max_iterations,
                capture_skill_dir=capture_skill_dir,
            )
        )

        # Write .upload_meta.json for each evolved skill
        for es in result.evolved_skills:
            if not isinstance(es, dict):
                continue
            skill_path = es.get("path", "")
            if skill_path:
                _write_upload_meta(Path(skill_path).parent, es)

        formatted = _format_task_result(result)
        if cloud_skill_candidates:
            formatted["cloud_skill_candidates"] = cloud_skill_candidates
            formatted["cloud_action_required"] = (
                "Cloud candidates were found but not imported automatically. "
                "Use cloud_browse_skills(action='local_taxonomy') and then "
                "cloud_browse_skills(action='import_skill', cloud_skill_id=..., "
                "local_category_path=...) before expecting them in local retrieval."
            )
        return _json_ok(formatted)

    except Exception as e:
        logger.error(f"execute_task failed: {e}", exc_info=True)
        return _json_error(e, status="error")


@mcp.tool()
async def search_skills(
    query: str,
    limit: int = 20,
) -> str:
    """Search skills in the local OpenSpace registry.

    Standalone local search for browsing / discovery. Use this when the bot
    wants to inspect skills that are already installed locally, then decide
    whether to handle the task locally or delegate to ``execute_task``.

    **Scope difference from execute_task**:
      - ``search_skills`` returns results to the bot for decision-making.
      - ``execute_task``'s internal search feeds directly into execution
        (the bot never sees the search results).

    For cloud package/skill browsing, use ``cloud_browse_skills``. Keeping
    cloud browsing out of this tool avoids overlapping agent choices.

    Uses hybrid ranking: BM25 → embedding re-rank → lexical boost.
    Embedding uses OpenAI when configured, then OpenRouter when configured,
    and falls back to lexical-only without a remote embedding provider.

    Args:
        query: Search query text (natural language or keywords).
        limit: Maximum results to return (default: 20).
    """
    try:
        from openspace.cloud.search import hybrid_search_skills

        q = query.strip()
        if not q:
            return _json_ok({"results": [], "count": 0})

        local_skills, store = await _get_local_search_context()

        results = await hybrid_search_skills(
            query=q,
            local_skills=local_skills,
            store=store,
            source="local",
            limit=limit,
        )

        return _json_ok({"results": results, "count": len(results), "source": "local"})

    except Exception as e:
        logger.error(f"search_skills failed: {e}", exc_info=True)
        return _json_error(e)


@mcp.tool()
async def cloud_browse_skills(
    action: str,
    query: str | None = None,
    search_id: str | None = None,
    package_ids: list[str] | None = None,
    package_id: str | None = None,
    cloud_skill_id: str | None = None,
    target_dir: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    limit: int = 8,
    audience: str = "requester_visible",
    artifact_filter: str = "downloadable_only",
    max_packages_per_pull: int = 40,
    max_skills_per_pull: int = 40,
) -> str:
    """Browse cloud packages/skills with one agent-facing stepwise tool.

    This is the cloud discovery entrypoint for agents. It keeps the LLM in the
    loop by returning package/skill summaries plus ``next_actions`` after each
    step. The agent continues by calling this same tool with the next action.

    Recommended action order:
      1. ``search_skills`` with ``query`` to rank concrete downloadable skills.
         Optionally pass ``package_id`` to restrict search to one package subtree.
      2. ``fetch_skill_detail`` with ``cloud_skill_id`` when exact metadata is
         needed before import.
      3. ``local_placement`` to choose or create a local package taxonomy path.
      4. ``import_skill`` with ``cloud_skill_id`` and ``local_category_path`` to
         download/register the selected skill locally.

    Optional package discovery actions:
      1. ``recall`` with ``query`` to get package candidates and ``search_id``.
      2. ``pull_projection`` with ``search_id`` + selected ``package_ids`` to
         inspect JSON package projections and record package-selection telemetry.
      3. ``search_skills`` with ``package_id`` to run skill-first search scoped
         to a chosen package.

    Optional: ``import_package_bundle`` with ``package_id`` downloads a selected
    package bundle when the agent needs outline files or bundled artifacts.

    Args:
        action: One of ``local_placement``, ``local_taxonomy``, ``recall``,
                ``pull_projection``, ``search_skills``, ``fetch_skill_detail``,
                ``import_skill``, or ``import_package_bundle``.
        query: Query text for ``search_skills`` and ``recall``.
        search_id: ``search_id`` returned by ``recall``.
        package_ids: Selected package ids for ``pull_projection``.
        package_id: One selected package id for package-local actions.
        cloud_skill_id: Selected cloud skill id for detail/import actions.
        target_dir: Optional local import root.
        local_category: Optional local skill type for ``import_skill``.
        local_category_path: Optional local package taxonomy path for
                             ``import_skill``. This is independent from cloud
                             package_path.
        limit: Max package or skill candidates, depending on action.
    """
    normalized_action = str(action or "").strip().lower().replace("-", "_")
    aliases = {
        "local_tree": "local_taxonomy",
        "inspect_local_tree": "local_taxonomy",
        "local_category_tree": "local_taxonomy",
        "placement": "local_placement",
        "local_place": "local_placement",
        "choose_local_path": "local_placement",
        "choose_local_category_path": "local_placement",
        "recall_packages": "recall",
        "pull": "pull_projection",
        "projection": "pull_projection",
        "package_projection": "pull_projection",
        "skill_search": "search_skills",
        "global_skill_search": "search_skills",
        "fetch": "fetch_skill_detail",
        "detail": "fetch_skill_detail",
        "import": "import_skill",
        "bundle": "import_package_bundle",
    }
    normalized_action = aliases.get(normalized_action, normalized_action)

    if normalized_action == "local_placement":
        return await cloud_local_placement(
            query=query,
            local_category=local_category,
            local_category_path=local_category_path,
            limit=limit,
        )
    if normalized_action == "local_taxonomy":
        return await cloud_local_taxonomy(
            query=query,
            local_category=local_category,
            local_category_path=local_category_path,
            limit=limit,
        )
    if normalized_action == "recall":
        return await cloud_recall_packages(
            query=query or "",
            limit=limit,
            audience=audience,
        )
    if normalized_action == "pull_projection":
        return await cloud_pull_package_projection(
            search_id=search_id or "",
            package_ids=package_ids or [],
            audience=audience,
            max_packages_per_pull=max_packages_per_pull,
            max_skills_per_pull=max_skills_per_pull,
        )
    if normalized_action == "search_skills":
        return await cloud_search_skills(
            query=query or "",
            package_id=package_id,
            limit=limit,
            audience=audience,
            artifact_filter=artifact_filter,
        )
    if normalized_action == "fetch_skill_detail":
        return await cloud_fetch_skill_detail(cloud_skill_id=cloud_skill_id or "")
    if normalized_action == "import_skill":
        return await cloud_import_skill(
            cloud_skill_id=cloud_skill_id or "",
            target_dir=target_dir,
            local_category=local_category,
            local_category_path=local_category_path,
        )
    if normalized_action == "import_package_bundle":
        return await cloud_import_package_bundle(
            package_id=package_id or "",
            target_dir=target_dir,
            audience=audience,
        )

    return _json_ok({
        "status": "error",
        "code": "UNKNOWN_CLOUD_BROWSE_ACTION",
        "message": "Unknown cloud_browse_skills action",
        "action": action,
        "valid_actions": [
            "local_placement",
            "local_taxonomy",
            "recall",
            "pull_projection",
            "search_skills",
            "fetch_skill_detail",
            "import_skill",
            "import_package_bundle",
        ],
        "recommended_sequence": [
            "recall",
            "pull_projection",
            "search_skills",
            "fetch_skill_detail",
            "local_placement",
            "import_skill",
        ],
    })


async def cloud_local_taxonomy(
    *,
    query: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    limit: int = 8,
) -> str:
    """Return a bounded local package taxonomy view for LLM placement."""

    try:
        return _json_ok(
            await _local_taxonomy_payload(
                query=query,
                local_category=local_category,
                local_category_path=local_category_path,
                limit=limit,
            )
        )
    except Exception as e:
        logger.error("cloud_local_taxonomy failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_local_placement(
    *,
    query: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    limit: int = 8,
) -> str:
    """Return an explicit local category-path placement flow for agents."""

    try:
        return _json_ok(
            await _local_placement_payload(
                query=query,
                local_category=local_category,
                local_category_path=local_category_path,
                limit=limit,
            )
        )
    except Exception as e:
        logger.error("cloud_local_placement failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def _local_placement_payload(
    *,
    query: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    taxonomy = await _local_taxonomy_payload(
        query=query,
        local_category=local_category,
        local_category_path=local_category_path,
        limit=limit,
    )
    candidates = _local_taxonomy_candidate_paths(taxonomy)
    payload = {
        **taxonomy,
        "status": "needs_local_category_path",
        "code": "LOCAL_CATEGORY_PATH_REQUIRED",
        "placement_decision_required": True,
        "interaction_flow": _local_placement_interaction_flow(),
        "local_category_path_policy": _local_category_path_policy(),
        "existing_path_candidates": candidates,
        "new_child_path_examples": _local_new_child_path_examples(candidates),
    }
    if str(local_category_path or "").strip():
        payload["candidate_local_category_path"] = str(local_category_path).strip()
        payload["status"] = "local_category_path_candidate"
        payload["code"] = "LOCAL_CATEGORY_PATH_CANDIDATE"
    payload["next_actions"] = [
        {
            "tool": "cloud_browse_skills",
            "action": "local_placement",
            "reason": "Expand one returned path, search by task/skill terms, or inspect another local taxonomy branch.",
            "optional_fields": ["local_category_path", "query", "local_category", "limit"],
        },
        {
            "tool": "cloud_browse_skills",
            "action": "import_skill",
            "reason": "For cloud imports, pass the selected or newly composed local_category_path.",
            "required_fields": ["cloud_skill_id", "local_category_path"],
            "optional_fields": ["local_category"],
        },
        {
            "consumer": "evolution_suggestion",
            "reason": "For DERIVED/CAPTURED generation, put the selected or newly composed path in decision.local_category_path.",
            "required_fields": ["local_category_path"],
        },
    ]
    return payload


async def _local_taxonomy_payload(
    *,
    query: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
    limit: int = 8,
) -> dict[str, Any]:
    from openspace.cloud.skill_classification import (
        build_local_taxonomy_snapshot,
        initialize_local_skill_taxonomy,
    )

    openspace = await _get_openspace()
    registry = openspace.get_skill_registry()
    local_skills = []
    if registry:
        local_skills = registry.list_skills()
    mapping_store = await _get_cloud_mapping_store()
    try:
        bootstrap = initialize_local_skill_taxonomy(
            mapping_store=mapping_store,
            skills=local_skills,
        )
        payload = build_local_taxonomy_snapshot(
            mapping_store=mapping_store,
            skills=local_skills,
            category=local_category,
            path_prefix=local_category_path,
            query=query,
            max_paths=min(max(int(limit), 1), 25),
        )
        payload["bootstrap"] = {
            "initialized_local_skill_count": bootstrap["created_count"],
            "skipped_local_skill_count": bootstrap["skipped_count"],
        }
    finally:
        mapping_store.close()
    payload["next_actions"] = [
        {
            "tool": "cloud_browse_skills",
            "action": "local_placement",
            "reason": "Use the explicit local placement flow before choosing or creating local_category_path.",
            "optional_fields": ["local_category", "local_category_path", "query", "limit"],
        },
        {
            "tool": "cloud_browse_skills",
            "action": "import_skill",
            "reason": "After selecting a local package taxonomy path, import the selected cloud skill.",
            "required_fields": ["cloud_skill_id", "local_category_path"],
        }
    ]
    return payload


def _local_placement_interaction_flow() -> list[dict[str, Any]]:
    return [
        {
            "step": "browse_roots_or_search",
            "call": "cloud_browse_skills(action='local_placement', query=... optional)",
            "agent_decision": "Inspect top-level local taxonomy roots or query-matched paths.",
        },
        {
            "step": "expand_one_branch",
            "call": "cloud_browse_skills(action='local_placement', local_category_path=...)",
            "agent_decision": "Expand one candidate path instead of requesting the full local tree.",
        },
        {
            "step": "choose_or_create",
            "agent_decision": (
                "Choose an existing existing_path_candidates[].local_category_path, "
                "or compose a nearby new child path under a returned candidate."
            ),
        },
        {
            "step": "use_selected_path",
            "call": (
                "cloud_browse_skills(action='import_skill', cloud_skill_id=..., "
                "local_category_path=...) or include local_category_path in a DERIVED/CAPTURED decision"
            ),
        },
    ]


def _local_category_path_policy() -> dict[str, Any]:
    return {
        "local_category_path_is_agent_selected": True,
        "path_shape": "package-style taxonomy path, for example technology/computing/browser-automation",
        "category_is_separate_skill_type": True,
        "independent_from_cloud_package_path": True,
        "allowed_forms": [
            {
                "form": "existing_local_taxonomy_path",
                "source": "existing_path_candidates[].local_category_path",
            },
            {
                "form": "new_child_local_taxonomy_path",
                "source": "existing_path_candidates[].local_category_path + '/' + one_new_segment",
            },
            {
                "form": "new_local_root_path",
                "source": "allowed when no returned root fits the skill/task; keep it package-style and specific",
            },
        ],
        "new_path_creation": {
            "allowed": True,
            "preferred_increment": "one new child segment under a returned candidate",
            "may_create_new_root": True,
            "does_not_create_cloud_package": True,
            "does_not_upload_or_download_anything": True,
        },
        "avoid": [
            "Using category-only paths such as workflow or tool_guide as taxonomy placement.",
            "Using a vague root when a more specific returned branch fits.",
            "Treating local_category_path as cloud_package_path; they are separate fields.",
        ],
    }


def _local_taxonomy_candidate_paths(taxonomy: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for key in ("paths", "children", "roots", "sample_paths"):
        value = taxonomy.get(key)
        if isinstance(value, list):
            rows.extend(item for item in value if isinstance(item, dict))
    seen: set[str] = set()
    candidates: list[dict[str, Any]] = []
    for row in rows:
        path = str(row.get("local_category_path") or "").strip()
        if not path or path in seen:
            continue
        seen.add(path)
        candidates.append({
            "local_category_path": path,
            "skill_count": int(row.get("skill_count") or 0),
            "skill_categories": dict(row.get("skill_categories") or {}),
            "review_states": dict(row.get("review_states") or {}),
            "sources": list(row.get("sources") or []),
            "examples": list(row.get("examples") or [])[:3],
        })
    return candidates[:25]


def _local_new_child_path_examples(
    candidates: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    for candidate in candidates:
        parent = str(candidate.get("local_category_path") or "").strip().strip("/")
        if not parent:
            continue
        examples.append({
            "parent_local_category_path": parent,
            "local_category_path_example": f"{parent}/<one-new-local-package-segment>",
        })
        if len(examples) >= limit:
            break
    return examples


async def cloud_recall_packages(
    query: str,
    limit: int = 8,
    audience: str = "requester_visible",
    task_id: str | None = None,
) -> str:
    """Recall cloud package candidates and return them for LLM selection.

    This is the first step of the LLM-guided cloud skill browsing flow.  It
    deliberately stops after ``POST /api/v2/recall/search`` so the agent can
    inspect package summaries and choose which package ids to pull next.

    Next typical call through the public tool:
    ``cloud_browse_skills(action="pull_projection", search_id=..., package_ids=...)``.
    """

    try:
        q = query.strip()
        if not q:
            return _json_ok({"status": "success", "search_id": "", "results": [], "count": 0})
        client = _get_cloud_client()
        payload = await asyncio.to_thread(
            client.search_packages,
            query=q,
            audience=audience,
            limit=min(max(int(limit), 1), 50),
            task_id=task_id,
        )
        results = [
            _summarize_recall_package(item)
            for item in (payload.get("results") or [])
            if isinstance(item, dict)
        ]
        return _json_ok({
            "status": "success",
            "query": q,
            "audience": payload.get("audience", audience),
            "search_id": payload.get("search_id", ""),
            "results": results,
            "count": len(results),
            "next_actions": [
                {
                    "tool": "cloud_browse_skills",
                    "action": "pull_projection",
                    "reason": "Inspect selected package projection and record package-selection telemetry.",
                    "required_fields": ["search_id", "package_ids"],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "search_skills",
                    "reason": "Narrow a known package directly to concrete downloadable skills.",
                    "required_fields": ["package_id", "query"],
                },
            ],
        })
    except Exception as e:
        logger.error("cloud_recall_packages failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_pull_package_projection(
    search_id: str,
    package_ids: list[str],
    audience: str = "requester_visible",
    max_packages_per_pull: int = 40,
    max_skills_per_pull: int = 40,
) -> str:
    """Pull JSON package projections selected from ``cloud_browse_skills(action="recall")``.

    This calls ``POST /api/v2/packages/pull``.  It returns package/skill
    summaries for the LLM to decide whether to search inside a package, import
    an exact skill, or import/download the package bundle for deeper outline
    inspection.
    """

    try:
        clean_ids = [str(package_id).strip() for package_id in (package_ids or []) if str(package_id).strip()]
        if not str(search_id or "").strip():
            return _json_error("search_id is required from cloud_recall_packages")
        if not clean_ids:
            return _json_error("package_ids must contain at least one package_id")
        client = _get_cloud_client()
        payload = await asyncio.to_thread(
            client.pull_packages,
            package_ids=clean_ids,
            search_id=str(search_id).strip(),
            audience=audience,
        )
        pulls = [
            _summarize_projection_pull(
                pull,
                max_packages=max(int(max_packages_per_pull), 1),
                max_skills=max(int(max_skills_per_pull), 1),
            )
            for pull in (payload.get("pulls") or [])
            if isinstance(pull, dict)
        ]
        return _json_ok({
            "status": "success",
            "search_id": payload.get("search_id", search_id),
            "audience": payload.get("audience", audience),
            "pulls": pulls,
            "count": len(pulls),
            "next_actions": [
                {
                    "tool": "cloud_browse_skills",
                    "action": "search_skills",
                    "reason": "Search concrete skills inside a chosen root_package_id.",
                    "required_fields": ["package_id", "query"],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "fetch_skill_detail",
                    "reason": "Inspect exact cloud skill metadata before import.",
                    "required_fields": ["cloud_skill_id"],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "local_taxonomy",
                    "reason": "Inspect the local package taxonomy before choosing where to import the cloud skill.",
                    "required_fields": [],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "import_skill",
                    "reason": "Download/register an exact selected cloud skill after choosing a local package taxonomy path.",
                    "required_fields": ["cloud_skill_id", "local_category_path"],
                    "optional_fields": ["local_category"],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "import_package_bundle",
                    "reason": "Download/import the package bundle when the agent wants the package outline and bundled artifacts.",
                    "required_fields": ["package_id"],
                },
            ],
        })
    except Exception as e:
        logger.error("cloud_pull_package_projection failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_search_skills(
    query: str,
    package_id: str | None = None,
    limit: int = 10,
    audience: str = "requester_visible",
    artifact_filter: str = "downloadable_only",
) -> str:
    """Search concrete cloud skills through ``POST /api/v2/skills/search``."""

    try:
        q = str(query or "").strip()
        pkg = str(package_id or "").strip()
        if not q:
            return _json_error("query is required")
        client = _get_cloud_client()
        payload = await asyncio.to_thread(
            client.search_skills,
            query=q,
            package_id=pkg or None,
            audience=audience,
            limit=min(max(int(limit), 1), 50),
            artifact_filter=artifact_filter,
        )
        results = [
            _summarize_cloud_skill_candidate(item)
            for item in (payload.get("results") or [])
            if isinstance(item, dict)
        ]
        return _json_ok({
            "status": "success",
            "endpoint": "/api/v2/skills/search",
            "package_id": pkg,
            "query": q,
            "audience": payload.get("audience", audience),
            "root_package_id": payload.get("root_package_id", pkg),
            "root_package_path": payload.get("root_package_path", ""),
            "skill_search_id": payload.get("skill_search_id", ""),
            "requested_mode": payload.get("requested_mode", ""),
            "served_mode": payload.get("served_mode", ""),
            "semantic_status": payload.get("semantic_status", ""),
            "fallback_reason": payload.get("fallback_reason", ""),
            "artifact_filter": artifact_filter,
            "results": results,
            "count": len(results),
            "next_actions": [
                {
                    "tool": "cloud_browse_skills",
                    "action": "fetch_skill_detail",
                    "reason": "Inspect exact metadata for a selected cloud_skill_id.",
                    "required_fields": ["cloud_skill_id"],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "local_taxonomy",
                    "reason": "Inspect the local package taxonomy before choosing where to import the cloud skill.",
                    "required_fields": [],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "import_skill",
                    "reason": "Download/register a selected cloud skill after choosing a local package taxonomy path.",
                    "required_fields": ["cloud_skill_id", "local_category_path"],
                    "optional_fields": ["local_category"],
                },
            ],
        })
    except Exception as e:
        logger.error("cloud_search_skills failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_fetch_skill_detail(
    cloud_skill_id: str,
) -> str:
    """Fetch exact cloud skill metadata before the LLM decides to import."""

    try:
        skill_id = str(cloud_skill_id or "").strip()
        if not skill_id:
            return _json_error("cloud_skill_id is required")
        client = _get_cloud_client()
        detail = await asyncio.to_thread(client.fetch_cloud_skill, skill_id)
        return _json_ok({
            "status": "success",
            "skill": detail,
            "summary": _summarize_cloud_skill_candidate(detail),
            "next_actions": [
                {
                    "tool": "cloud_browse_skills",
                    "action": "local_taxonomy",
                    "reason": "Inspect the local package taxonomy before choosing where to import this cloud skill.",
                    "required_fields": [],
                },
                {
                    "tool": "cloud_browse_skills",
                    "action": "import_skill",
                    "reason": "Download/register this exact cloud skill after choosing a local package taxonomy path.",
                    "required_fields": ["cloud_skill_id", "local_category_path"],
                    "optional_fields": ["local_category"],
                }
            ],
        })
    except Exception as e:
        logger.error("cloud_fetch_skill_detail failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_import_skill(
    cloud_skill_id: str,
    target_dir: str | None = None,
    local_category: str | None = None,
    local_category_path: str | None = None,
) -> str:
    """Download and register one exact cloud skill selected by the agent.

    ``local_category_path`` is the agent-selected local package taxonomy path.
    It is independent from the cloud package path stored in the binding.
    """

    try:
        skill_id = str(cloud_skill_id or "").strip()
        if not skill_id:
            return _json_error("cloud_skill_id is required")
        if not str(local_category_path or "").strip():
            client = _get_cloud_client()
            try:
                detail = await asyncio.to_thread(client.fetch_cloud_skill, skill_id)
            except Exception:
                detail = {"cloud_skill_id": skill_id}
            taxonomy_query = " ".join(
                str(detail.get(key) or "")
                for key in ("title", "name", "summary", "description", "package_path")
            ).strip()
            return _json_ok({
                "status": "needs_local_category_path",
                "code": "LOCAL_CATEGORY_PATH_REQUIRED",
                "message": (
                    "Read the local package taxonomy, then choose local_category_path "
                    "before importing a cloud skill."
                ),
                "policy_decision_required": True,
                "cloud_skill_id": skill_id,
                "cloud_skill": _summarize_cloud_skill_candidate(detail),
                "local_placement": await _local_placement_payload(
                    query=taxonomy_query or None,
                    limit=8,
                ),
                "next_actions": [
                    {
                        "tool": "cloud_browse_skills",
                        "action": "local_placement",
                        "reason": "Browse or expand the local placement flow if the initial candidates are insufficient.",
                        "optional_fields": ["local_category", "local_category_path", "query", "limit"],
                    },
                    {
                        "tool": "cloud_browse_skills",
                        "action": "import_skill",
                        "reason": "Import after the agent selects a local package taxonomy path from/under the local tree.",
                        "required_fields": ["cloud_skill_id", "local_category_path"],
                        "optional_fields": ["local_category"],
                    }
                ],
            })
        result = await _do_import_cloud_skill(
            skill_id,
            target_dir=target_dir,
            local_category=local_category,
            local_category_path=local_category_path,
        )
        return _json_ok({
            **result,
            "status": result.get("status", "success"),
            "next_actions": [
                {
                    "tool": "search_skills",
                    "reason": "Confirm the imported skill participates in local retrieval.",
                    "required_fields": ["query", "source"],
                }
            ],
        })
    except Exception as e:
        logger.error("cloud_import_skill failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


async def cloud_import_package_bundle(
    package_id: str,
    target_dir: str | None = None,
    audience: str = "requester_visible",
) -> str:
    """Download/import a selected cloud package bundle for deeper inspection.

    This is intentionally separate from search.  Use it after the LLM has
    selected a package and wants the package bundle index/outline/artifacts
    locally rather than only JSON projection metadata.
    """

    try:
        pkg = str(package_id or "").strip()
        if not pkg:
            return _json_error("package_id is required")
        client = _get_cloud_client()
        base_dir = _resolve_cloud_import_base_dir(target_dir)
        result = await asyncio.to_thread(
            client.import_package_bundle,
            pkg,
            base_dir,
            audience=audience,
        )
        local_path = str(result.get("local_path") or "").strip()
        package_dir = Path(local_path) if local_path else None
        if package_dir is not None and package_dir.exists():
            openspace = await _get_openspace()
            _register_evidence_read_roots(openspace, package_dir)
            registry = openspace.get_skill_registry()
            if registry:
                discovered = registry.discover_from_dirs([package_dir])
                store = await _get_runtime_store(required=False)
                if store and discovered:
                    await store.sync_from_registry(discovered)
                result["registered_skill_count"] = len(discovered)
        result.setdefault("registered_skill_count", 0)
        return _json_ok(result)
    except Exception as e:
        logger.error("cloud_import_package_bundle failed: %s", e, exc_info=True)
        return _json_error(e, status="error")


@mcp.tool()
async def fix_skill(
    skill_dir: str,
    direction: str,
) -> str:
    """Run a manual FIX evolution job for a broken skill.

    This endpoint creates durable evidence and a TriggerJob, then asks the
    runtime to claim and process that exact job through the evolution engine.
    It does not directly author, validate, commit, write SkillStore mutation
    state, or prepare upload metadata.

    The skill does not need to be pre-registered in OpenSpace.  Provide the
    skill directory path and OpenSpace registers it before creating the job.

    Args:
        skill_dir: Path to the broken skill directory (must contain SKILL.md).
        direction: What's broken and how to fix it.  Be specific:
                   e.g. "The upstream endpoint changed path" or
                   "Add retry logic for HTTP 429 rate limit errors".
    """
    try:
        from openspace.skill_engine.triggers import ManualTriggerRequest

        if not direction:
            return _json_error("direction is required — describe what to fix.")

        skill_path = Path(skill_dir)
        skill_md = skill_path / "SKILL.md"
        if not skill_md.exists():
            return _json_error(f"SKILL.md not found in {skill_dir}")

        openspace = await _get_openspace()
        registry = openspace.get_skill_registry()
        if not registry:
            return _json_error("SkillRegistry not initialized")
        trigger_engine = openspace.get_trigger_engine()
        if not trigger_engine:
            return _json_error("Evolution TriggerEngine is not initialized")

        meta = registry.register_skill_dir(skill_path)
        if not meta:
            return _json_error(f"Failed to register skill from {skill_dir}")

        store = await _get_runtime_store()
        await store.sync_from_registry([meta])
        _register_evidence_read_roots(openspace, skill_path)

        request = ManualTriggerRequest(
            action="fix",
            reason="manual_fix",
            skill_ids=(meta.skill_id,),
            metadata={
                "skill_dir": str(skill_path),
                "direction": direction,
                "skill_name": meta.name,
            },
        )
        jobs = trigger_engine.from_manual_request(request)
        if not jobs:
            return _json_ok({
                "status": "rejected",
                "error": "Manual fix request did not produce a TriggerJob.",
                "skill_id": meta.skill_id,
            })

        outcomes = []
        get_evolution_engine = getattr(openspace, "get_evolution_engine", None)
        evolution_engine = (
            get_evolution_engine() if callable(get_evolution_engine) else None
        )
        runtime = getattr(openspace, "runtime", None)
        drain_evolution_jobs = getattr(runtime, "drain_evolution_jobs", None)
        run_now_available = evolution_engine is not None and callable(
            drain_evolution_jobs
        )
        if run_now_available:
            outcomes = await drain_evolution_jobs(
                job_ids=[job.job_id for job in jobs],
                limit=len(jobs),
            )

        outcome_summaries = [_evolution_run_summary(outcome) for outcome in outcomes]
        fix_result = _manual_fix_result(
            outcomes,
            evolution_mode=getattr(
                getattr(runtime, "config", None),
                "evolution_mode",
                "autonomous",
            ),
            engine_available=run_now_available,
        )

        return _json_ok({
            **fix_result,
            "target_skill_id": meta.skill_id,
            "skill_id": fix_result.get("skill_id") or meta.skill_id,
            "skill_dir": str(skill_path),
            "jobs": [job.to_dict() for job in jobs],
            "outcomes": outcome_summaries,
        })

    except Exception as e:
        logger.error(f"fix_skill failed: {e}", exc_info=True)
        return _json_error(e, status="error")


def _manual_fix_result(
    outcomes: list[Any],
    *,
    evolution_mode: str,
    engine_available: bool,
) -> dict[str, Any]:
    if not engine_available:
        return {
            "status": "failed",
            "reason": "evolution_engine_unavailable",
            "retryable": True,
        }

    summaries = [_evolution_run_summary(outcome) for outcome in outcomes]
    for summary in summaries:
        committed_actions = [
            action
            for action in summary.get("actions", [])
            if action.get("commit_status") in {"committed", "committed_reconciled"}
        ]
        if committed_actions or summary["evolved_skill_ids"]:
            action = committed_actions[0] if committed_actions else {}
            skill_id = (
                action.get("skill_id")
                or (summary["evolved_skill_ids"][0] if summary["evolved_skill_ids"] else "")
            )
            return {
                "status": "fixed",
                "action_id": action.get("action_id") or (
                    summary["action_ids"][0] if summary["action_ids"] else ""
                ),
                "skill_id": skill_id,
                "changed_files": action.get("changed_files", []),
                "upload_metadata_refs": action.get("upload_metadata_refs", []),
                "retryable": False,
            }

    rejected = _manual_fix_rejection(summaries)
    if rejected is not None:
        return rejected

    failed = _manual_fix_failure(summaries)
    if failed is not None:
        return failed

    mode = str(evolution_mode or "autonomous").strip().lower()
    if mode == "audit_only" and any(summary["admission_ids"] for summary in summaries):
        return {
            "status": "accepted_audit_only",
            "reason": "evolution_mode_audit_only",
            "decision_ids": [
                item for summary in summaries for item in summary["decision_ids"]
            ],
            "admission_ids": [
                item for summary in summaries for item in summary["admission_ids"]
            ],
            "retryable": False,
        }

    return {
        "status": "failed",
        "reason": "manual_fix_job_produced_no_committed_action",
        "retryable": True,
    }


def _evolution_run_summary(outcome: Any) -> dict[str, Any]:
    decisions = list(getattr(outcome, "decisions", []) or [])
    admissions = list(getattr(outcome, "admissions", []) or [])
    candidates = list(getattr(outcome, "candidates", []) or [])
    actions = list(getattr(outcome, "actions", []) or [])
    evolved = list(getattr(outcome, "evolved_skill_records", []) or [])
    return {
        "job_id": str(getattr(outcome, "job_id", "") or ""),
        "status": str(getattr(outcome, "status", "") or ""),
        "decisions": [
            {
                "decision_id": str(getattr(item, "decision_id", "") or ""),
                "proposed_action": str(getattr(item, "proposed_action", "") or ""),
                "candidate_policy": str(getattr(item, "candidate_policy", "") or ""),
                "noop_reason": str(getattr(item, "noop_reason", "") or ""),
            }
            for item in decisions
        ],
        "decision_ids": [
            str(getattr(item, "decision_id", "") or "")
            for item in decisions
            if getattr(item, "decision_id", None)
        ],
        "admissions": [
            {
                "admission_id": str(getattr(item, "admission_id", "") or ""),
                "outcome": str(getattr(item, "outcome", "") or ""),
                "hard_failures": [
                    str(value)
                    for value in (getattr(item, "hard_failures", []) or [])
                ],
                "warnings": [
                    str(value)
                    for value in (getattr(item, "warnings", []) or [])
                ],
            }
            for item in admissions
        ],
        "admission_ids": [
            str(getattr(item, "admission_id", "") or "")
            for item in admissions
            if getattr(item, "admission_id", None)
        ],
        "candidate_ids": [
            str(getattr(item, "candidate_id", "") or "")
            for item in candidates
            if getattr(item, "candidate_id", None)
        ],
        "actions": [
            {
                "action_id": str(getattr(item, "action_id", "") or ""),
                "validation_id": str(getattr(item, "validation_id", "") or ""),
                "commit_status": str(getattr(item, "commit_status", "") or ""),
                "skill_id": str(getattr(item, "skill_id", "") or ""),
                "changed_files": [
                    str(value)
                    for value in (getattr(item, "changed_files", []) or [])
                ],
                "failure_reason": str(getattr(item, "failure_reason", "") or ""),
                "upload_metadata_refs": [
                    str(value)
                    for value in (
                        getattr(item, "upload_metadata_refs", []) or []
                    )
                ],
            }
            for item in actions
        ],
        "action_ids": [
            str(getattr(item, "action_id", "") or "")
            for item in actions
            if getattr(item, "action_id", None)
        ],
        "validation_ids": [
            str(getattr(item, "validation_id", "") or "")
            for item in actions
            if getattr(item, "validation_id", None)
        ],
        "evolved_skill_ids": [
            str(getattr(item, "skill_id", "") or "")
            for item in evolved
            if getattr(item, "skill_id", None)
        ],
        "errors": [str(item) for item in (getattr(outcome, "errors", []) or [])],
    }


def _manual_fix_rejection(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    rejected_admissions: list[dict[str, Any]] = []
    for summary in summaries:
        for admission in summary.get("admissions", []):
            if admission.get("outcome") in {
                "reject",
                "rejected",
                "noop",
                "needs_human_review",
                "human_review",
            }:
                rejected_admissions.append(admission)
    if not rejected_admissions:
        return None
    reasons: list[str] = []
    for admission in rejected_admissions:
        reasons.extend(admission.get("hard_failures", []))
        reasons.extend(admission.get("warnings", []))
    return {
        "status": "rejected",
        "reason": "; ".join(reason for reason in reasons if reason)[:1000]
        or "admission_or_validation_rejected",
        "admission_ids": [
            item["admission_id"]
            for item in rejected_admissions
            if item.get("admission_id")
        ],
        "retryable": False,
    }


def _manual_fix_failure(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
    errors: list[str] = []
    retryable = False
    for summary in summaries:
        if str(summary.get("status", "")).startswith("failed"):
            errors.extend(summary.get("errors", []))
            retryable = True
        for action in summary.get("actions", []):
            commit_status = str(action.get("commit_status") or "")
            if commit_status and commit_status not in {
                "committed",
                "committed_reconciled",
            }:
                errors.append(action.get("failure_reason") or commit_status)
                retryable = commit_status in {"failed", "failed_needs_review"}
    if not errors:
        return None
    return {
        "status": "failed",
        "reason": "; ".join(error for error in errors if error)[:1000]
        or "manual_fix_job_failed",
        "retryable": retryable,
    }


async def _resolve_and_save_upload_placement(
    skill_path: Path,
    cloud_package_path: str,
    *,
    origin: str | None = None,
    mapping_store: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    normalized_path = str(cloud_package_path or "").strip().strip("/")
    if not normalized_path:
        return {
            "status": "error",
            "code": "PACKAGE_PATH_REQUIRED",
            "message": "cloud_package_path is required for non-fix uploads",
        }

    if mapping_store is None:
        mapping_store = await _get_cloud_mapping_store()
    if client is None:
        client = _get_cloud_client(mapping_store=mapping_store)

    from openspace.cloud.package_placement import (
        PackagePlacementError,
        PackagePlacementResolver,
    )

    try:
        placement = PackagePlacementResolver(
            client,
            mapping_store=mapping_store,
        ).resolve_cloud_package_path(normalized_path)
    except PackagePlacementError as exc:
        return exc.to_payload()

    meta = await _read_upload_meta(skill_path)
    if not isinstance(meta, dict):
        meta = {}
    if origin is not None:
        meta["origin"] = _normalize_upload_origin(origin)
    else:
        meta.setdefault("origin", "imported")
    upload_placement = {
        "requested_package_id": placement.requested_package_id,
        "requested_parent_package_id": placement.requested_parent_package_id,
        "requested_new_package_segment": placement.requested_new_package_segment,
        "snapshot_version_used": placement.snapshot_version_used,
        "root_sub_domain_package_id": placement.root_sub_domain_package_id,
        "package_path": placement.package_path,
    }
    meta["upload_placement"] = upload_placement
    _save_upload_meta(skill_path, meta)

    return {
        "status": "success",
        "skill_dir": str(skill_path),
        "cloud_package_path": placement.package_path or normalized_path,
        "upload_placement": upload_placement,
    }


async def _resolve_and_save_upload_placement_fields(
    skill_path: Path,
    *,
    requested_package_id: str | None = None,
    requested_parent_package_id: str | None = None,
    requested_new_package_segment: str | None = None,
    origin: str | None = None,
    mapping_store: Any | None = None,
    client: Any | None = None,
) -> dict[str, Any]:
    if mapping_store is None:
        mapping_store = await _get_cloud_mapping_store()
    if client is None:
        client = _get_cloud_client(mapping_store=mapping_store)

    from openspace.cloud.package_placement import (
        PackagePlacementError,
        PackagePlacementResolver,
    )

    try:
        placement = PackagePlacementResolver(
            client,
            mapping_store=mapping_store,
        ).validate_confirmed_placement(
            requested_package_id=str(requested_package_id or "").strip() or None,
            requested_parent_package_id=str(requested_parent_package_id or "").strip() or None,
            requested_new_package_segment=str(requested_new_package_segment or "").strip() or None,
        )
    except PackagePlacementError as exc:
        return exc.to_payload()

    meta = await _read_upload_meta(skill_path)
    if not isinstance(meta, dict):
        meta = {}
    if origin is not None:
        meta["origin"] = _normalize_upload_origin(origin)
    else:
        meta.setdefault("origin", "imported")
    upload_placement = {
        "requested_package_id": placement.requested_package_id,
        "requested_parent_package_id": placement.requested_parent_package_id,
        "requested_new_package_segment": placement.requested_new_package_segment,
        "snapshot_version_used": placement.snapshot_version_used,
        "root_sub_domain_package_id": placement.root_sub_domain_package_id,
        "package_path": placement.package_path,
    }
    meta["upload_placement"] = upload_placement
    _save_upload_meta(skill_path, meta)

    return {
        "status": "success",
        "skill_dir": str(skill_path),
        "cloud_package_path": placement.package_path,
        "upload_placement": upload_placement,
    }


async def prepare_upload_placement(
    skill_dir: str,
    cloud_package_path: str,
    origin: str | None = None,
) -> str:
    """Resolve a cloud package path and save confirmed upload placement.

    Internal compatibility helper. The agent-facing path is now
    ``upload_skill(cloud_package_path=...)``; this helper remains available to
    tests and direct Python callers.

    Args:
        skill_dir: Path to skill directory (must contain SKILL.md).
        cloud_package_path: Domain/sub-domain/regular package path. Existing
                            regular packages are selected directly; a single
                            missing child regular package can be prepared when
                            the parent allows child creation.
        origin: Optional upload origin to persist alongside placement.
    """
    try:
        skill_path = Path(skill_dir)
        if not (skill_path / "SKILL.md").exists():
            return _json_error(f"SKILL.md not found in {skill_dir}")
        payload = await _resolve_and_save_upload_placement(
            skill_path,
            cloud_package_path,
            origin=origin,
        )
        if payload.get("status") == "success":
            payload["next_action"] = "upload_skill"
        return _json_ok(payload)
    except Exception as e:
        logger.error(f"prepare_upload_placement failed: {e}", exc_info=True)
        return _json_error(e, status="error")


async def _upload_cloud_tree_payload(
    *,
    client: Any,
    skill_path: Path,
    cloud_sub_domain_package_id: str | None = None,
    cloud_package_query: str | None = None,
    cloud_package_path_prefix: str | None = None,
    limit: int = 12,
) -> dict[str, Any]:
    subdomain_id = str(cloud_sub_domain_package_id or "").strip()
    max_items = min(max(int(limit), 1), 50)
    package_query = str(cloud_package_query or "").strip()
    package_prefix = str(cloud_package_path_prefix or "").strip().strip("/")
    domain_payload = await asyncio.to_thread(client.get_package_domain_index)
    snapshot_version = str(domain_payload.get("snapshot_version") or "")
    domain_nodes = [
        _summarize_upload_package_node(node)
        for node in (domain_payload.get("nodes") or [])
        if isinstance(node, dict)
    ]
    domain_node_matches = _filter_upload_package_nodes(
        domain_nodes,
        query=package_query,
        prefix=package_prefix,
        limit=max_items,
    )
    sub_domain_matches = _filter_upload_package_nodes(
        [node for node in domain_nodes if node.get("package_kind") == "sub-domain"],
        query=package_query,
        prefix=package_prefix,
        limit=max_items,
    )
    payload: dict[str, Any] = {
        "status": "needs_cloud_package_path",
        "code": "CLOUD_PACKAGE_PATH_REQUIRED",
        "message": (
            "Inspect the cloud package tree, then call upload_skill with "
            "cloud_package_id for an existing regular package, or "
            "cloud_parent_package_id plus cloud_new_package_segment to create "
            "one new child regular package. cloud_package_path is also accepted "
            "when the server returns a non-empty package path."
        ),
        "policy_decision_required": True,
        "skill_dir": str(skill_path),
        "snapshot_version": snapshot_version,
        "query": package_query,
        "path_prefix": package_prefix,
        "interaction_flow": _upload_cloud_package_interaction_flow(),
        "cloud_package_path_policy": _upload_cloud_package_path_policy(),
        "domain_index": {
            "snapshot_version": snapshot_version,
            "total_node_count": len(domain_nodes),
            "nodes": domain_node_matches["items"],
            "nodes_truncated": domain_node_matches["truncated"],
            "sub_domain_nodes": sub_domain_matches["items"],
            "sub_domain_count": sub_domain_matches["match_count"],
            "sub_domain_nodes_truncated": sub_domain_matches["truncated"],
        },
        "next_actions": [
            {
                "tool": "upload_skill",
                "reason": "Inspect a selected sub-domain upload subtree before choosing cloud_package_path.",
                "required_fields": ["skill_dir", "cloud_sub_domain_package_id"],
                "optional_fields": ["cloud_package_query", "cloud_package_path_prefix", "cloud_package_limit"],
            },
            {
                "tool": "upload_skill",
                "reason": "Upload after choosing a regular package id from the upload tree.",
                "required_fields": ["skill_dir", "cloud_package_id"],
            },
            {
                "tool": "upload_skill",
                "reason": "Upload while creating one new regular child package under a chosen parent.",
                "required_fields": ["skill_dir", "cloud_parent_package_id", "cloud_new_package_segment"],
            },
        ],
    }
    if not subdomain_id:
        return payload

    subtree = await asyncio.to_thread(
        client.get_package_subtree_for_upload,
        subdomain_id,
        snapshot_version=snapshot_version or None,
    )
    subtree_nodes = [
        _summarize_upload_package_node(node)
        for node in (subtree.get("nodes") or [])
        if isinstance(node, dict)
    ]
    selectable = _filter_upload_package_nodes(
        [node for node in subtree_nodes if node.get("can_select_as_upload_target")],
        query=package_query,
        prefix=package_prefix,
        limit=max_items,
    )
    creatable_parent_nodes = [
        node for node in subtree_nodes if _can_create_upload_child(node)
    ]
    creatable = _filter_upload_package_nodes(
        creatable_parent_nodes,
        query=package_query,
        prefix=package_prefix,
        limit=max_items,
    )
    child_view = _upload_package_children(
        subtree_nodes,
        prefix=package_prefix or str(subtree.get("root_package_path") or ""),
        limit=max_items,
    )
    new_child_examples = _upload_new_child_path_examples(
        creatable["items"],
        limit=min(max_items, 5),
    )
    payload["subtree"] = {
        "root_sub_domain_package_id": (
            subtree.get("root_sub_domain_package_id")
            or subtree.get("root_package_id")
            or subdomain_id
        ),
        "root_package_path": subtree.get("root_package_path", ""),
        "snapshot_version": subtree.get("snapshot_version", snapshot_version),
        "total_node_count": len(subtree_nodes),
        "children": child_view["items"],
        "child_count": child_view["match_count"],
        "children_truncated": child_view["truncated"],
        "selectable_regular_packages": selectable["items"],
        "selectable_regular_package_count": selectable["match_count"],
        "selectable_regular_packages_truncated": selectable["truncated"],
        "creatable_parent_packages": creatable["items"],
        "creatable_parent_package_count": creatable["match_count"],
        "creatable_parent_packages_truncated": creatable["truncated"],
        "new_child_path_examples": new_child_examples,
    }
    payload["next_actions"] = [
        {
            "tool": "upload_skill",
            "reason": "Upload after choosing an existing regular package id, or creating one new child under a creatable parent.",
            "accepted_forms": [
                {
                    "required_fields": ["skill_dir", "cloud_package_id"],
                    "source": "subtree.selectable_regular_packages[].package_id",
                },
                {
                    "required_fields": ["skill_dir", "cloud_parent_package_id", "cloud_new_package_segment"],
                    "source": "subtree.creatable_parent_packages[].package_id plus one new segment",
                },
                {
                    "required_fields": ["skill_dir", "cloud_package_path"],
                    "source": "only when subtree package_path is non-empty",
                },
            ],
        }
    ]
    return payload


def _upload_cloud_package_interaction_flow() -> list[dict[str, Any]]:
    return [
        {
            "step": "browse_domain_index",
            "call": "upload_skill(skill_dir=..., cloud_package_query=... optional)",
            "agent_decision": "Choose a cloud_sub_domain_package_id to inspect.",
        },
        {
            "step": "browse_upload_subtree",
            "call": "upload_skill(skill_dir=..., cloud_sub_domain_package_id=..., cloud_package_path_prefix=... optional)",
            "agent_decision": (
                "Choose an existing selectable_regular_packages[].package_id, "
                "or choose creatable_parent_packages[].package_id plus one new regular segment. "
                "Use package_path only when it is non-empty."
            ),
        },
        {
            "step": "upload",
            "call": "upload_skill(skill_dir=..., visibility=..., cloud_package_id=...) or upload_skill(skill_dir=..., visibility=..., cloud_parent_package_id=..., cloud_new_package_segment=...)",
            "result": (
                "OpenSpace saves UUID placement fields in .upload_meta.json, "
                "revalidates them against the current cloud tree, then uploads."
            ),
        },
    ]


def _upload_cloud_package_path_policy() -> dict[str, Any]:
    return {
        "cloud_package_path_is_agent_selected": True,
        "code_does_not_choose_semantic_destination": True,
        "allowed_forms": [
            {
                "form": "existing_regular_package_id",
                "source": "subtree.selectable_regular_packages[].package_id",
                "upload_fields": ["requested_package_id"],
            },
            {
                "form": "new_child_regular_package_id_plus_segment",
                "source": "subtree.creatable_parent_packages[].package_id + one_new_regular_package_segment",
                "upload_fields": [
                    "requested_parent_package_id",
                    "requested_new_package_segment",
                ],
            },
            {
                "form": "existing_or_new_child_path",
                "source": "cloud_package_path when the server exposes a non-empty package_path",
                "upload_fields": ["requested_package_id or requested_parent_package_id/requested_new_package_segment"],
            },
        ],
        "new_path_creation": {
            "allowed": True,
            "max_missing_segments": 1,
            "new_segment_kind": "regular package",
            "parent_requirement": (
                "Parent must be a sub-domain or regular package that does not "
                "explicitly disable child regular package creation."
            ),
            "segment_must_be_single_path_part": True,
        },
        "not_allowed": [
            "Uploading directly to a domain or sub-domain path.",
            "Creating multiple missing path segments in one upload.",
            "Passing only a raw path without letting upload_skill resolve and revalidate placement.",
        ],
    }


def _can_create_upload_child(node: dict[str, Any]) -> bool:
    kind = str(node.get("package_kind") or "")
    if kind not in {"sub-domain", "regular"}:
        return False
    return node.get("can_create_child_regular_package") is not False


def _upload_new_child_path_examples(
    parent_nodes: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for node in parent_nodes:
        parent_path = str(node.get("package_path") or "").strip().strip("/")
        if not parent_path:
            continue
        examples.append({
            "parent_package_id": node.get("package_id", ""),
            "parent_package_path": parent_path,
            "cloud_package_path_example": (
                f"{parent_path}/<one-new-regular-package-segment>"
            ),
        })
        if len(examples) >= limit:
            break
    return examples


def _summarize_upload_package_node(node: dict[str, Any]) -> dict[str, Any]:
    return {
        "package_id": node.get("package_id", ""),
        "package_path": node.get("package_path", ""),
        "package_kind": node.get("package_kind", ""),
        "parent_package_id": node.get("parent_package_id"),
        "root_sub_domain_package_id": node.get("root_sub_domain_package_id"),
        "can_select_as_upload_target": bool(node.get("can_select_as_upload_target")),
        "can_create_child_regular_package": node.get("can_create_child_regular_package"),
        "select_disabled_reason": node.get("select_disabled_reason"),
        "child_count": node.get("child_count"),
    }


def _filter_upload_package_nodes(
    nodes: list[dict[str, Any]],
    *,
    query: str,
    prefix: str,
    limit: int,
) -> dict[str, Any]:
    q_tokens = [token for token in str(query or "").lower().split() if token]
    normalized_prefix = str(prefix or "").strip().strip("/").lower()
    matches: list[dict[str, Any]] = []
    for node in nodes:
        package_path = str(node.get("package_path") or "")
        if normalized_prefix and not package_path.lower().startswith(normalized_prefix):
            continue
        if q_tokens:
            text = "\n".join(
                str(node.get(key) or "")
                for key in (
                    "package_id",
                    "package_path",
                    "package_kind",
                    "select_disabled_reason",
                )
            ).lower()
            if not all(token in text for token in q_tokens):
                continue
        matches.append(node)
    return {
        "items": matches[:limit],
        "match_count": len(matches),
        "truncated": len(matches) > limit,
    }


def _upload_package_children(
    nodes: list[dict[str, Any]],
    *,
    prefix: str,
    limit: int,
) -> dict[str, Any]:
    prefix_parts = [
        part.strip().lower()
        for part in str(prefix or "").replace("\\", "/").split("/")
        if part.strip()
    ]
    children: dict[str, dict[str, Any]] = {}
    for node in nodes:
        raw_path = str(node.get("package_path") or "")
        path_parts = [part.strip() for part in raw_path.replace("\\", "/").split("/") if part.strip()]
        lowered = [part.lower() for part in path_parts]
        if prefix_parts:
            if len(lowered) <= len(prefix_parts) or lowered[: len(prefix_parts)] != prefix_parts:
                continue
            child_parts = path_parts[: len(prefix_parts) + 1]
        else:
            if not path_parts:
                continue
            child_parts = path_parts[:1]
        child_path = "/".join(child_parts)
        existing = children.get(child_path)
        if existing is None:
            existing = {
                "package_path": child_path,
                "package_kind": "",
                "package_id": "",
                "subtree_package_count": 0,
                "selectable_regular_package_count": 0,
                "can_create_child_regular_package": False,
            }
            children[child_path] = existing
        existing["subtree_package_count"] += 1
        if node.get("can_select_as_upload_target"):
            existing["selectable_regular_package_count"] += 1
        if node.get("can_create_child_regular_package"):
            existing["can_create_child_regular_package"] = True
        if lowered == [part.lower() for part in child_parts]:
            existing["package_id"] = node.get("package_id", "")
            existing["package_kind"] = node.get("package_kind", "")
            existing["can_select_as_upload_target"] = bool(node.get("can_select_as_upload_target"))
            existing["select_disabled_reason"] = node.get("select_disabled_reason")
    rows = sorted(children.values(), key=lambda item: str(item.get("package_path") or ""))
    return {
        "items": rows[:limit],
        "match_count": len(rows),
        "truncated": len(rows) > limit,
    }


@mcp.tool()
async def upload_skill(
    skill_dir: str,
    visibility: str = "private",
    cloud_package_path: str | None = None,
    cloud_package_id: str | None = None,
    cloud_parent_package_id: str | None = None,
    cloud_new_package_segment: str | None = None,
    cloud_sub_domain_package_id: str | None = None,
    cloud_package_query: str | None = None,
    cloud_package_path_prefix: str | None = None,
    cloud_package_limit: int = 12,
    origin: str | None = None,
    parent_local_skill_ids: list[str] | None = None,
    owner_agent_id: str | None = None,
    submitted_skill_id: str | None = None,
    content_diff: str | None = None,
) -> str:
    """Upload a trusted local skill to the cloud.

    Public and private uploads both fail closed unless ``skill_dir`` resolves
    to a matching trusted record in the active local SkillStore. Trust remains
    local metadata and is not sent in the cloud upload request.

    For evolved skills from validated evolution actions, lineage
    metadata is **pre-saved** in ``.upload_meta.json``.  The bot provides:

      - ``skill_dir`` — path to the skill directory
      - ``visibility`` — "private" by default, or "public" when explicitly sharing
      - package placement for non-fix uploads without pre-saved placement

    For non-fix uploads, this tool is also the agent-facing cloud package
    browser.  Calling it without confirmed placement returns a bounded
    step-by-step picker payload; calling it with ``cloud_sub_domain_package_id``
    expands one upload subtree.  After the agent chooses an existing regular
    package id, or chooses an eligible parent package id plus one new child
    segment, the tool saves UUID placement fields in ``.upload_meta.json`` and
    revalidates the placement immediately before upload.  ``cloud_package_path``
    is also accepted when the cloud returns a non-empty path.

    **origin + parent_local_skill_ids constraints**:
      - imported / captured → parent_local_skill_ids must be empty
      - derived → at least 1 parent with cloud binding
      - fixed → exactly 1 parent with cloud binding

    Args:
        skill_dir: Path to skill directory (must contain SKILL.md).
        visibility: "public" or "private". Defaults to "private"; choose
                    "public" only when explicitly sharing.
        cloud_package_path: Agent-selected existing regular package path, or
                            one new child regular package segment under an
                            eligible parent. Required for non-fix uploads
                            unless upload_placement already exists in
                            .upload_meta.json.
        cloud_package_id: Agent-selected existing regular package id from
                          selectable_regular_packages[].package_id.
        cloud_parent_package_id: Agent-selected parent package id from
                                 creatable_parent_packages[].package_id when
                                 creating one new regular child package.
        cloud_new_package_segment: New child regular package segment to create
                                   under cloud_parent_package_id.
        cloud_sub_domain_package_id: Optional sub-domain package id. If provided
                                     without confirmed placement, this returns
                                     the upload subtree for the agent to inspect.
        cloud_package_query: Optional filter when browsing cloud package choices.
        cloud_package_path_prefix: Optional cloud package path prefix to expand/filter.
        cloud_package_limit: Maximum cloud package candidates returned while browsing.
        origin: Override origin.  Default: from .upload_meta.json or "imported".
        parent_local_skill_ids: Override local parents.  Default: from .upload_meta.json/SkillStore.
    """
    try:
        from openspace.cloud.local_mapping import (
            UnboundLocalSkillError,
            ensure_local_skill_id,
            read_local_skill_id,
            write_local_skill_id,
        )

        skill_path = Path(skill_dir)
        if not (skill_path / "SKILL.md").exists():
            return _json_error(f"SKILL.md not found in {skill_dir}")

        from openspace.cloud.upload_trust import (
            SkillUploadTrustError,
            require_trusted_skill_for_upload,
        )

        runtime_store = await _get_runtime_store(required=False)
        try:
            require_trusted_skill_for_upload(
                skill_path,
                skill_store=runtime_store,
            )
        except SkillUploadTrustError as exc:
            return _json_ok(exc.to_payload())

        # Read pre-saved metadata (written after validated evolution commits)
        meta = await _read_upload_meta(skill_path)

        # Merge: explicit params override pre-saved values
        final_origin = origin if origin is not None else meta.get("origin", "imported")
        final_parent_local_ids = (
            parent_local_skill_ids
            if parent_local_skill_ids is not None
            else meta.get("parent_local_skill_ids", [])
        )
        final_parent_local_ids = [sid for sid in (final_parent_local_ids or []) if sid]
        final_owner_agent_id = owner_agent_id if owner_agent_id is not None else meta.get("owner_agent_id")
        existing_local_skill_id = read_local_skill_id(skill_path)
        final_submitted_skill_id = submitted_skill_id or meta.get("local_skill_id")
        if final_submitted_skill_id and existing_local_skill_id and final_submitted_skill_id != existing_local_skill_id:
            return _json_ok({
                "status": "error",
                "code": "LOCAL_SKILL_ID_MISMATCH",
                "message": "submitted_skill_id must match the local .skill_id",
                "submitted_skill_id": final_submitted_skill_id,
                "local_skill_id": existing_local_skill_id,
            })
        if final_submitted_skill_id and not existing_local_skill_id:
            write_local_skill_id(skill_path, final_submitted_skill_id)
        local_skill_id = final_submitted_skill_id or ensure_local_skill_id(skill_path)
        final_content_diff = content_diff if content_diff is not None else meta.get("content_diff")

        mapping_store = await _get_cloud_mapping_store()
        origin_type = _normalize_upload_origin(final_origin)
        if origin_type in ("fix", "derive"):
            try:
                final_parent_cloud_ids = mapping_store.resolve_parent_local_ids_to_cloud_ids(
                    final_parent_local_ids
                )
            except UnboundLocalSkillError as exc:
                return _json_ok(exc.to_payload())
        else:
            if final_parent_local_ids:
                return _json_ok({
                    "status": "error",
                    "code": "PARENT_LOCAL_IDS_NOT_ALLOWED",
                    "message": "imported/capture uploads must not include parent local skill IDs",
                    "parent_local_skill_ids": final_parent_local_ids,
            })
            final_parent_cloud_ids = []
        client = _get_cloud_client(mapping_store=mapping_store)
        id_placement_selected = bool(
            str(cloud_package_id or "").strip()
            or str(cloud_parent_package_id or "").strip()
            or str(cloud_new_package_segment or "").strip()
        )
        if cloud_package_path and id_placement_selected:
            return _json_ok({
                "status": "error",
                "code": "PACKAGE_PLACEMENT_CONFLICTING_FIELDS",
                "message": (
                    "Use either cloud_package_path, cloud_package_id, or "
                    "cloud_parent_package_id plus cloud_new_package_segment."
                ),
            })
        if id_placement_selected:
            placement_payload = await _resolve_and_save_upload_placement_fields(
                skill_path,
                requested_package_id=cloud_package_id,
                requested_parent_package_id=cloud_parent_package_id,
                requested_new_package_segment=cloud_new_package_segment,
                origin=origin,
                mapping_store=mapping_store,
                client=client,
            )
            if placement_payload.get("status") != "success":
                return _json_ok(placement_payload)
            meta = await _read_upload_meta(skill_path)
        if cloud_package_path:
            placement_payload = await _resolve_and_save_upload_placement(
                skill_path,
                cloud_package_path,
                origin=origin,
                mapping_store=mapping_store,
                client=client,
            )
            if placement_payload.get("status") != "success":
                return _json_ok(placement_payload)
            meta = await _read_upload_meta(skill_path)

        placement_kwargs: dict[str, str | None] = {
            "requested_package_id": None,
            "requested_parent_package_id": None,
            "requested_new_package_segment": None,
            "snapshot_version_used": None,
        }
        if origin_type != "fix" or isinstance(meta.get("upload_placement"), dict):
            from openspace.cloud.package_placement import (
                PackagePlacementError,
                PackagePlacementResolver,
                placement_from_upload_meta,
            )

            raw_placement = placement_from_upload_meta(meta)
            if not raw_placement:
                return _json_ok(
                    await _upload_cloud_tree_payload(
                        client=client,
                        skill_path=skill_path,
                        cloud_sub_domain_package_id=cloud_sub_domain_package_id,
                        cloud_package_query=cloud_package_query,
                        cloud_package_path_prefix=cloud_package_path_prefix,
                        limit=cloud_package_limit,
                    )
                )
            try:
                resolved_placement = PackagePlacementResolver(
                    client,
                    mapping_store=mapping_store,
                ).validate_confirmed_placement(**raw_placement)
            except PackagePlacementError as exc:
                return _json_ok(exc.to_payload())
            placement_kwargs = resolved_placement.to_upload_kwargs()
        result = await asyncio.to_thread(
            client.upload_skill_v2,
            skill_path,
            local_skill_store_db_path=getattr(runtime_store, "db_path", None),
            visibility=visibility,
            origin=final_origin,
            parent_cloud_skill_ids=final_parent_cloud_ids,
            **placement_kwargs,
            owner_agent_id=final_owner_agent_id,
            submitted_skill_id=local_skill_id,
            content_diff=final_content_diff,
        )
        result["local_skill_id"] = local_skill_id
        if final_parent_local_ids:
            result["parent_local_skill_ids"] = final_parent_local_ids
        if final_parent_cloud_ids:
            result["parent_cloud_skill_ids"] = final_parent_cloud_ids
        return _json_ok(result)

    except Exception as e:
        logger.error(f"upload_skill failed: {e}", exc_info=True)
        return _json_error(e, status="error")

def run_mcp_server() -> None:
    """Console-script entry point for ``openspace-mcp``."""
    import argparse

    def _port_flag_was_set(argv: list[str]) -> bool:
        return any(arg == "--port" or arg.startswith("--port=") for arg in argv)

    def _default_port_for_transport(transport: str) -> int:
        return 8081 if transport == "streamable-http" else 8080

    def _parse_port_from_env(default: int) -> int:
        raw_port = os.environ.get("OPENSPACE_MCP_PORT", "").strip()
        if not raw_port:
            return default
        try:
            return int(raw_port)
        except ValueError:
            logger.warning(
                "Ignoring invalid OPENSPACE_MCP_PORT=%r; falling back to %d.",
                raw_port,
                default,
            )
            return default

    def _parse_host_from_env(default: str = "127.0.0.1") -> str:
        return os.environ.get("OPENSPACE_MCP_HOST", "").strip() or default

    def _resolve_transport(requested_transport: str, argv: list[str]) -> str:
        if requested_transport in ("stdio", "sse", "streamable-http"):
            return requested_transport

        env_transport = os.environ.get("OPENSPACE_MCP_TRANSPORT", "").strip().lower()
        if env_transport:
            if env_transport in ("stdio", "sse", "streamable-http"):
                return env_transport
            logger.warning(
                "Ignoring invalid OPENSPACE_MCP_TRANSPORT=%r; expected 'stdio', 'sse', or 'streamable-http'.",
                env_transport,
            )

        # Treat an explicit port override as an HTTP/SSE intent. This keeps the
        # CLI behavior aligned with the usage examples above.
        if _port_flag_was_set(argv):
            return "sse"

        stdin_is_tty = hasattr(sys.stdin, "isatty") and sys.stdin.isatty()
        stdout_is_tty = _real_stdout.isatty()
        return "sse" if stdin_is_tty and stdout_is_tty else "stdio"

    argv = sys.argv[1:]
    parser = argparse.ArgumentParser(description="OpenSpace MCP Server")
    parser.add_argument(
        "--transport",
        choices=["auto", "stdio", "sse", "streamable-http"],
        default="auto",
    )
    parser.add_argument("--host", default=_parse_host_from_env())
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    transport = _resolve_transport(args.transport, argv)
    port = args.port
    if port is None:
        port = _parse_port_from_env(_default_port_for_transport(transport))

    if transport == "sse":
        mcp.settings.host = args.host
        mcp.settings.port = port
        logger.info("Starting OpenSpace MCP server with SSE transport on port %s", port)
        mcp.run(transport="sse")
    elif transport == "streamable-http":
        mcp.settings.host = args.host
        mcp.settings.port = port
        logger.info(
            "Starting OpenSpace MCP server with streamable HTTP transport on %s:%s",
            args.host,
            port,
        )
        mcp.run(transport="streamable-http")
    else:
        logger.info("Starting OpenSpace MCP server with stdio transport")
        mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()
