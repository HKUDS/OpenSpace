from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from openspace.services.runtime_support.settings import get_openspace_config_home_dir


ENTER_PLAN_MODE_TOOL_NAME = "EnterPlanMode"
EXIT_PLAN_MODE_TOOL_NAME = "ExitPlanMode"

PLAN_MODE_ATTACHMENT_TURNS_BETWEEN_ATTACHMENTS = 5
PLAN_MODE_FULL_REMINDER_EVERY_N_ATTACHMENTS = 5

_PLAN_SLUGS_BY_SESSION: dict[str, str] = {}
_ACTIVE_PLAN_PREFIXES: set[str] = set()


def _session_key(session_id: str | None) -> str:
    return str(session_id or "default")


def _sanitize_slug(value: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return sanitized or "plan"


def get_plans_directory(config_home: str | Path | None = None) -> Path:
    path = get_openspace_config_home_dir(config_home) / "plans"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_plan_slug(session_id: str | None = None) -> str:
    key = _session_key(session_id)
    cached = _PLAN_SLUGS_BY_SESSION.get(key)
    if cached:
        return cached
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:10]
    prefix = _sanitize_slug(key)[:40]
    slug = f"{prefix}-{digest}" if prefix != digest else digest
    _PLAN_SLUGS_BY_SESSION[key] = slug
    return slug


def set_plan_slug(session_id: str | None, slug: str) -> None:
    _PLAN_SLUGS_BY_SESSION[_session_key(session_id)] = _sanitize_slug(slug)


def clear_plan_slug(session_id: str | None = None) -> None:
    _PLAN_SLUGS_BY_SESSION.pop(_session_key(session_id), None)


def get_plan_file_path(
    session_id: str | None = None,
    agent_id: str | None = None,
    *,
    config_home: str | Path | None = None,
) -> Path:
    slug = get_plan_slug(session_id)
    suffix = ""
    if agent_id and agent_id != "primary":
        suffix = f"-agent-{_sanitize_slug(str(agent_id))}"
    path = get_plans_directory(config_home) / f"{slug}{suffix}.md"
    register_active_plan_path(path, session_id=session_id)
    return path


def get_plan(
    session_id: str | None = None,
    agent_id: str | None = None,
    *,
    config_home: str | Path | None = None,
) -> str | None:
    path = get_plan_file_path(session_id, agent_id, config_home=config_home)
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def write_plan(
    content: str,
    session_id: str | None = None,
    agent_id: str | None = None,
    *,
    config_home: str | Path | None = None,
) -> Path:
    path = get_plan_file_path(session_id, agent_id, config_home=config_home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    register_active_plan_path(path, session_id=session_id)
    return path


def register_active_plan_path(path: str | Path, *, session_id: str | None = None) -> None:
    try:
        resolved = Path(path).expanduser().resolve()
    except OSError:
        resolved = Path(path).expanduser().absolute()
    _ACTIVE_PLAN_PREFIXES.add(str(resolved))
    if session_id:
        try:
            base = get_plans_directory() / get_plan_slug(session_id)
            _ACTIVE_PLAN_PREFIXES.add(str(base.expanduser().resolve()))
        except OSError:
            pass


def is_active_plan_file(path: str | Path) -> bool:
    try:
        resolved = str(Path(path).expanduser().resolve())
    except OSError:
        resolved = str(Path(path).expanduser().absolute())
    if resolved in _ACTIVE_PLAN_PREFIXES:
        return True
    for prefix in tuple(_ACTIVE_PLAN_PREFIXES):
        if resolved.startswith(prefix) and resolved.endswith(".md"):
            return True
    return False


def register_plan_mode_permission_predicates() -> None:
    try:
        from openspace.grounding.core.permissions.filesystem import (
            register_internal_path_predicate,
        )
    except Exception:
        return

    marker = "_openspace_plan_mode_predicates_registered"
    if getattr(register_plan_mode_permission_predicates, marker, False):
        return
    register_internal_path_predicate(
        category="editable",
        reason="Plan files for current session are allowed for writing",
        predicate=is_active_plan_file,
    )
    register_internal_path_predicate(
        category="readable",
        reason="Plan files for current session are allowed for reading",
        predicate=is_active_plan_file,
    )
    setattr(register_plan_mode_permission_predicates, marker, True)


def enter_plan_mode(context: Any) -> dict[str, Any]:
    previous = str(getattr(context, "permission_mode", None) or "default")
    if previous == "plan":
        previous = str(getattr(context, "pre_plan_mode", None) or "default")
    setattr(context, "pre_plan_mode", previous)
    setattr(context, "permission_mode", "plan")
    perm_ctx = getattr(context, "permission_context", None)
    if perm_ctx is not None:
        from dataclasses import replace

        context.permission_context = replace(
            perm_ctx,
            mode="plan",
            pre_plan_mode=previous,
        )
    session_id = getattr(context, "session_id", None)
    agent_id = getattr(context, "agent_id", None)
    plan_path = get_plan_file_path(session_id, agent_id)
    setattr(context, "plan_file_path", str(plan_path))
    setattr(context, "plan_mode_exited_in_session", False)
    return {
        "previous_mode": previous,
        "plan_file_path": str(plan_path),
    }


def exit_plan_mode(context: Any) -> dict[str, Any]:
    previous = str(getattr(context, "pre_plan_mode", None) or "default")
    if previous == "plan":
        previous = "default"
    setattr(context, "permission_mode", previous)
    setattr(context, "pre_plan_mode", None)
    setattr(context, "plan_mode_exit_pending", True)
    setattr(context, "plan_mode_exited_in_session", True)
    perm_ctx = getattr(context, "permission_context", None)
    if perm_ctx is not None:
        from dataclasses import replace

        context.permission_context = replace(
            perm_ctx,
            mode=previous,
            pre_plan_mode=None,
        )
    return {"restored_mode": previous}


register_plan_mode_permission_predicates()

