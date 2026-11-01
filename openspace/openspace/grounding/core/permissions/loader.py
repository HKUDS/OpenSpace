"""Permission settings loader.

Key persistence behavior:

* "Always allow" UI updates default to ``localSettings`` at
  ``<cwd>/.openspace/settings.local.json``.
* ``session`` / ``cliArg`` — runtime-only stores (process-lifetime singletons,
  never persisted).  See :class:`_BehaviorRuleStore`.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from openspace.utils.logging import Logger
from openspace.services.runtime_support.settings import (
    get_settings_for_source as _settings_get_settings_for_source,
    get_settings_path_for_source as _settings_get_settings_path_for_source,
    save_settings_for_source as _settings_save_settings_for_source,
)

from .types import (
    EXTERNAL_PERMISSION_MODES,
    PERMISSION_RULE_SOURCES,
    AddDirectoriesUpdate,
    AdditionalWorkingDirectory,
    AddRulesUpdate,
    ExternalPermissionMode,
    PermissionBehavior,
    PermissionMode,
    PermissionRule,
    PermissionRuleSource,
    PermissionRuleValue,
    PermissionUpdate,
    PermissionUpdateDestination,
    RemoveDirectoriesUpdate,
    RemoveRulesUpdate,
    ReplaceRulesUpdate,
    SetModeUpdate,
    ToolPermissionContext,
    ToolPermissionRulesBySource,
    format_rule_value,
    parse_rule_value,
)

logger = Logger.get_logger(__name__)


# ════════════════════════════════════════════════════════════════════════
# §1  Constants
# ════════════════════════════════════════════════════════════════════════

_SUPPORTED_RULE_BEHAVIORS: Tuple[PermissionBehavior, ...] = ("allow", "deny", "ask")

_EDITABLE_SOURCES: Tuple[PermissionUpdateDestination, ...] = (
    "userSettings",
    "projectSettings",
    "localSettings",
)

# ════════════════════════════════════════════════════════════════════════
# §2  Settings file paths
# ════════════════════════════════════════════════════════════════════════


def _settings_path_for_source(
    source: PermissionRuleSource, cwd: str
) -> Optional[Path]:
    """Return the on-disk path for a source, or None if the source is
    runtime-only (``session`` / ``cliArg``).

    Implementation: ``getSettingsFilePathForSource`` in settings.ts.
    """
    if source in ("userSettings", "projectSettings", "localSettings"):
        return _settings_get_settings_path_for_source(source, cwd)
    # session / cliArg: runtime stores, handled separately.
    return None


def get_settings_for_source(
    source: PermissionRuleSource, cwd: str
) -> Optional[Dict[str, Any]]:
    """OpenSpace ``getSettingsForSource`` — return parsed JSON for a source.

    Returns ``None`` when:
    * the source is runtime-only (``session`` / ``cliArg``);
    * the source has no on-disk path;
    * the file does not exist;
    * the file is unreadable or contains invalid JSON (a warning is
      logged but no exception propagates — parity with OpenSpace's lenient
      loader used for editing).

    An empty file returns ``{}`` (same as OpenSpace).
    """
    if source in ("session", "cliArg"):
        return None

    try:
        return _settings_get_settings_for_source(source, cwd)
    except Exception as exc:
        logger.warning(
            "Failed to read settings for source %s at %s: %s",
            source,
            _settings_path_for_source(source, cwd),
            exc,
        )
        return None


def save_settings_for_source(
    source: PermissionRuleSource, cwd: str, settings: Mapping[str, Any]
) -> None:
    """OpenSpace ``updateSettingsForSource`` — atomically write ``settings`` JSON.

    Raises :class:`ValueError` when ``source`` is not writable
    (runtime stores).  Parent
    directory is created on demand.  Write is atomic (temp-file + rename)
    to survive process crashes mid-write.
    """
    if source in ("session", "cliArg"):
        raise ValueError(
            f"source {source!r} is a runtime store; use the store APIs instead"
        )
    if source not in _EDITABLE_SOURCES:
        raise ValueError(f"source {source!r} is not editable in OpenSpace")

    path = _settings_path_for_source(source, cwd)
    if path is None:
        raise ValueError(f"no settings path defined for source {source!r}")

    _settings_save_settings_for_source(source, dict(settings), cwd=cwd)


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Write ``data`` as JSON to ``path`` via a tempfile + rename.

    The tempfile is created in the same directory as ``path`` so the
    rename is atomic on POSIX; if the write fails, the tempfile is
    cleaned up.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        prefix=".settings-",
        suffix=".json.tmp",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=False)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except OSError:
            pass
        raise


# ════════════════════════════════════════════════════════════════════════
# §3  Runtime stores (session / cliArg)
# ════════════════════════════════════════════════════════════════════════


class _BehaviorRuleStore:
    """Process-level singleton for runtime-only permission sources.

    Used by the ``session`` and ``cliArg`` rule sources, which OpenSpace keeps
    in memory only — they never touch the disk.  Thread-safe via a
    single coarse-grained lock (permissions are updated at most a few
    times per turn, so contention is negligible).

    Stores three things:

    * per-behavior rule list (``allow`` / ``deny`` / ``ask``);
    * additional working directories (paths added via ``addDirectories``
      updates with this destination);
    * a default-mode override (used when cli flag ``--permission-mode``
      is passed — OpenSpace maps the flag to a ``session``-scoped ``setMode``
      update).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: Dict[PermissionBehavior, List[PermissionRuleValue]] = {
            "allow": [],
            "deny": [],
            "ask": [],
        }
        self._directories: List[str] = []
        self._mode: Optional[ExternalPermissionMode] = None

    def add_rule(
        self, behavior: PermissionBehavior, rule: PermissionRuleValue
    ) -> None:
        with self._lock:
            existing = {format_rule_value(r) for r in self._rules[behavior]}
            if format_rule_value(rule) not in existing:
                self._rules[behavior].append(rule)

    def remove_rule(
        self, behavior: PermissionBehavior, rule: PermissionRuleValue
    ) -> None:
        target = format_rule_value(rule)
        with self._lock:
            self._rules[behavior] = [
                r for r in self._rules[behavior] if format_rule_value(r) != target
            ]

    def replace_rules(
        self,
        behavior: PermissionBehavior,
        rules: Iterable[PermissionRuleValue],
    ) -> None:
        deduped: List[PermissionRuleValue] = []
        seen: set = set()
        for r in rules:
            s = format_rule_value(r)
            if s not in seen:
                seen.add(s)
                deduped.append(r)
        with self._lock:
            self._rules[behavior] = deduped

    def get_rules(
        self, behavior: PermissionBehavior
    ) -> List[PermissionRuleValue]:
        with self._lock:
            return list(self._rules[behavior])

    def add_directory(self, path: str) -> None:
        with self._lock:
            if path not in self._directories:
                self._directories.append(path)

    def remove_directory(self, path: str) -> None:
        with self._lock:
            self._directories = [d for d in self._directories if d != path]

    def get_directories(self) -> List[str]:
        with self._lock:
            return list(self._directories)

    def set_mode(self, mode: Optional[ExternalPermissionMode]) -> None:
        with self._lock:
            self._mode = mode

    def get_mode(self) -> Optional[ExternalPermissionMode]:
        with self._lock:
            return self._mode

    def clear(self) -> None:
        with self._lock:
            self._rules = {"allow": [], "deny": [], "ask": []}
            self._directories = []
            self._mode = None


_SESSION_STORE = _BehaviorRuleStore()
_CLIARG_STORE = _BehaviorRuleStore()


def _store_for_source(
    source: PermissionRuleSource,
) -> Optional[_BehaviorRuleStore]:
    if source == "session":
        return _SESSION_STORE
    if source == "cliArg":
        return _CLIARG_STORE
    return None


def get_session_store() -> _BehaviorRuleStore:
    """Return the live session rule store (for tests / TUI inspection)."""
    return _SESSION_STORE


def get_cliarg_store() -> _BehaviorRuleStore:
    """Return the live cliArg rule store."""
    return _CLIARG_STORE


def reset_runtime_stores() -> None:
    """Clear both runtime stores — used by tests for isolation."""
    _SESSION_STORE.clear()
    _CLIARG_STORE.clear()


# ════════════════════════════════════════════════════════════════════════
# §4  Parse helpers (OpenSpace settingsJsonToRules + PermissionUpdate roundtrip)
# ════════════════════════════════════════════════════════════════════════


def _normalize_rule_string(raw: str) -> Optional[str]:
    """Roundtrip parse→format so legacy names (``Bash(npm:*)``) compare
    equal to their canonical OS form (``bash(npm:*)``).

    Returns ``None`` when the string is malformed.  Callers drop ``None``
    entries (OpenSpace does the same via ``settingsJsonToRules`` silently
    skipping invalid rules).
    """
    try:
        return format_rule_value(parse_rule_value(raw))
    except ValueError:
        return None


def _rules_from_settings(
    settings: Optional[Mapping[str, Any]], source: PermissionRuleSource
) -> List[PermissionRule]:
    """OpenSpace ``settingsJsonToRules`` — permissionsLoader.ts L91-L114.

    Extracts allow/deny/ask arrays from the ``permissions`` object and
    turns each string into a :class:`PermissionRule` tagged with the
    given ``source``.  Malformed strings are logged-and-skipped (OpenSpace
    also silently skips them).
    """
    if not settings:
        return []
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return []

    rules: List[PermissionRule] = []
    for behavior in _SUPPORTED_RULE_BEHAVIORS:
        arr = perms.get(behavior)
        if not isinstance(arr, list):
            continue
        for raw in arr:
            if not isinstance(raw, str):
                continue
            try:
                value = parse_rule_value(raw)
            except ValueError as exc:
                logger.warning(
                    "Skipping malformed rule %r in source %s: %s",
                    raw,
                    source,
                    exc,
                )
                continue
            rules.append(
                PermissionRule(
                    source=source,
                    rule_behavior=behavior,
                    rule_value=value,
                )
            )
    return rules


def _additional_dirs_from_settings(
    settings: Optional[Mapping[str, Any]],
) -> List[str]:
    """Read ``permissions.additionalDirectories`` (OpenSpace's canonical place).

    For backward-compat with OS docs that show it at top-level, we also
    check top-level ``additionalDirectories`` if the nested one is
    missing.  Only string entries are kept.
    """
    if not settings:
        return []
    perms = settings.get("permissions")
    if isinstance(perms, dict):
        nested = perms.get("additionalDirectories")
        if isinstance(nested, list):
            return [d for d in nested if isinstance(d, str)]
    top_level = settings.get("additionalDirectories")
    if isinstance(top_level, list):
        return [d for d in top_level if isinstance(d, str)]
    return []


def _default_mode_from_settings(
    settings: Optional[Mapping[str, Any]],
) -> Optional[ExternalPermissionMode]:
    """Read ``permissions.defaultMode`` (OpenSpace canonical) with top-level
    fallback.  Invalid enum values are ignored.
    """
    if not settings:
        return None
    perms = settings.get("permissions")
    if isinstance(perms, dict):
        mode = perms.get("defaultMode")
        if isinstance(mode, str) and mode in EXTERNAL_PERMISSION_MODES:
            return mode  # type: ignore[return-value]
    mode_top = settings.get("defaultMode")
    if isinstance(mode_top, str) and mode_top in EXTERNAL_PERMISSION_MODES:
        return mode_top  # type: ignore[return-value]
    return None


# ════════════════════════════════════════════════════════════════════════
# §5  load_permissions_from_source / load_all_permission_rules_from_disk
# ════════════════════════════════════════════════════════════════════════


def _store_as_rules(
    store: _BehaviorRuleStore, source: PermissionRuleSource
) -> List[PermissionRule]:
    out: List[PermissionRule] = []
    for behavior in _SUPPORTED_RULE_BEHAVIORS:
        for value in store.get_rules(behavior):
            out.append(
                PermissionRule(
                    source=source,
                    rule_behavior=behavior,
                    rule_value=value,
                )
            )
    return out


def load_permissions_from_source(
    source: PermissionRuleSource, cwd: str
) -> List[PermissionRule]:
    """OpenSpace ``getPermissionRulesForSource`` — permissionsLoader.ts L140-L145.

    Load rules from a *single* source.  Disk sources read via
    :func:`get_settings_for_source`; runtime stores (``session`` /
    ``cliArg``) read from the process singletons.
    """
    store = _store_for_source(source)
    if store is not None:
        return _store_as_rules(store, source)
    settings = get_settings_for_source(source, cwd)
    return _rules_from_settings(settings, source)


def load_all_permission_rules_from_disk(
    cwd: str,
) -> Tuple[
    ToolPermissionRulesBySource,
    ToolPermissionRulesBySource,
    ToolPermissionRulesBySource,
]:
    """OpenSpace ``loadAllPermissionRulesFromDisk`` — permissionsLoader.ts L120-L133.

    Returns a triple ``(allow, deny, ask)`` of source-keyed dicts.  Each
    dict maps :data:`PermissionRuleSource` → tuple of formatted rule
    strings (canonical ``tool(content)`` form).  A source whose
    behavior bucket is empty is omitted from that dict.

    OpenSpace iterates :data:`PERMISSION_RULE_SOURCES`, which contains all
    sources in priority order.
    """
    allow: Dict[PermissionRuleSource, Tuple[str, ...]] = {}
    deny: Dict[PermissionRuleSource, Tuple[str, ...]] = {}
    ask: Dict[PermissionRuleSource, Tuple[str, ...]] = {}

    for source in PERMISSION_RULE_SOURCES:
        rules = load_permissions_from_source(source, cwd)
        if not rules:
            continue

        bucketed: Dict[PermissionBehavior, List[str]] = {
            "allow": [],
            "deny": [],
            "ask": [],
        }
        for rule in rules:
            bucketed[rule.rule_behavior].append(format_rule_value(rule.rule_value))

        if bucketed["allow"]:
            allow[source] = tuple(bucketed["allow"])
        if bucketed["deny"]:
            deny[source] = tuple(bucketed["deny"])
        if bucketed["ask"]:
            ask[source] = tuple(bucketed["ask"])

    return allow, deny, ask


# ════════════════════════════════════════════════════════════════════════
# §6  add / delete rules in settings files
# ════════════════════════════════════════════════════════════════════════


def add_permission_rules_to_settings(
    destination: PermissionUpdateDestination,
    rules: Iterable[PermissionRuleValue],
    behavior: PermissionBehavior,
    cwd: str,
) -> bool:
    """OpenSpace ``addPermissionRulesToSettings`` — permissionsLoader.ts L229-L296.

    Appends ``rules`` (deduped via normalized roundtrip) to the
    destination's rule list for the given ``behavior``.  Behavior by
    destination:

    * ``session`` / ``cliArg`` — append to runtime store (in-memory only);
    * ``userSettings`` / ``projectSettings`` / ``localSettings`` —
      read current settings JSON, dedupe, write back atomically;
    * any other destination — returns ``False`` with a warning.

    Existing JSON keys are preserved (parity with OpenSpace's spread-copy of
    ``settingsData``).
    """
    rule_list = list(rules)
    if not rule_list:
        return True  # OpenSpace returns true for no-op

    if destination == "session":
        for rule in rule_list:
            _SESSION_STORE.add_rule(behavior, rule)
        return True
    if destination == "cliArg":
        for rule in rule_list:
            _CLIARG_STORE.add_rule(behavior, rule)
        return True

    if destination not in _EDITABLE_SOURCES:
        logger.warning(
            "Cannot add permission rules to non-editable source %r",
            destination,
        )
        return False

    settings = get_settings_for_source(destination, cwd)
    if settings is None:
        settings = {}

    permissions_raw = settings.get("permissions")
    permissions = dict(permissions_raw) if isinstance(permissions_raw, dict) else {}

    existing_raw = permissions.get(behavior)
    existing_list: List[Any] = list(existing_raw) if isinstance(existing_raw, list) else []

    # Build the set of already-present canonical rule strings so we
    # don't re-append duplicates (OpenSpace does the same via Set + normalize).
    existing_canonical: set = set()
    for raw in existing_list:
        if isinstance(raw, str):
            canon = _normalize_rule_string(raw)
            if canon is not None:
                existing_canonical.add(canon)

    additions: List[str] = []
    for rule in rule_list:
        s = format_rule_value(rule)
        if s not in existing_canonical:
            existing_canonical.add(s)
            additions.append(s)

    if not additions:
        return True  # nothing to do after dedupe

    permissions[behavior] = existing_list + additions
    updated_settings = dict(settings)
    updated_settings["permissions"] = permissions

    try:
        save_settings_for_source(destination, cwd, updated_settings)
        return True
    except Exception as exc:  # pragma: no cover — exercised in error paths
        logger.warning(
            "Failed to persist permission rules to %s: %s", destination, exc
        )
        return False


def delete_permission_rule_from_settings(
    destination: PermissionUpdateDestination,
    rule: PermissionRuleValue,
    behavior: PermissionBehavior,
    cwd: str,
) -> bool:
    """OpenSpace ``deletePermissionRuleFromSettings`` — permissionsLoader.ts L163-L216.

    Deletes a single rule from the destination's bucket for ``behavior``.
    Returns ``True`` iff a rule was actually removed (OpenSpace semantics: no-op
    when the rule isn't there).  Legacy names match their canonical
    form thanks to the parse→format normalisation.
    """
    target = format_rule_value(rule)

    if destination == "session":
        existing = [format_rule_value(r) for r in _SESSION_STORE.get_rules(behavior)]
        if target not in existing:
            return False
        _SESSION_STORE.remove_rule(behavior, rule)
        return True
    if destination == "cliArg":
        existing = [format_rule_value(r) for r in _CLIARG_STORE.get_rules(behavior)]
        if target not in existing:
            return False
        _CLIARG_STORE.remove_rule(behavior, rule)
        return True

    if destination not in _EDITABLE_SOURCES:
        return False

    settings = get_settings_for_source(destination, cwd)
    if not settings:
        return False

    permissions_raw = settings.get("permissions")
    if not isinstance(permissions_raw, dict):
        return False
    behavior_raw = permissions_raw.get(behavior)
    if not isinstance(behavior_raw, list):
        return False

    new_list: List[Any] = []
    removed = False
    for raw in behavior_raw:
        if isinstance(raw, str):
            canon = _normalize_rule_string(raw)
            if canon == target:
                removed = True
                continue
        new_list.append(raw)

    if not removed:
        return False

    permissions = dict(permissions_raw)
    permissions[behavior] = new_list
    updated_settings = dict(settings)
    updated_settings["permissions"] = permissions

    try:
        save_settings_for_source(destination, cwd, updated_settings)
        return True
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to delete permission rule from %s: %s", destination, exc
        )
        return False


# ════════════════════════════════════════════════════════════════════════
# §7  apply_permission_update — context mutation (no disk)
# ════════════════════════════════════════════════════════════════════════


def _bucket_name_for_behavior(behavior: PermissionBehavior) -> str:
    """Map ``allow``/``deny``/``ask`` → ``always_*_rules`` attribute."""
    if behavior == "allow":
        return "always_allow_rules"
    if behavior == "deny":
        return "always_deny_rules"
    return "always_ask_rules"


def _clone_context_with(
    context: ToolPermissionContext, **overrides: Any
) -> ToolPermissionContext:
    """Return a shallow copy of ``context`` with the given fields replaced.

    Needed because :class:`ToolPermissionContext` is a frozen dataclass
    — every mutation produces a new instance (OpenSpace does the same via the
    ``{...context, field: new}`` spread pattern).
    """
    fields = dict(
        mode=context.mode,
        additional_working_directories=context.additional_working_directories,
        always_allow_rules=context.always_allow_rules,
        always_deny_rules=context.always_deny_rules,
        always_ask_rules=context.always_ask_rules,
        is_bypass_permissions_mode_available=context.is_bypass_permissions_mode_available,
        stripped_dangerous_rules=context.stripped_dangerous_rules,
        should_avoid_permission_prompts=context.should_avoid_permission_prompts,
        await_automated_checks_before_dialog=context.await_automated_checks_before_dialog,
        pre_plan_mode=context.pre_plan_mode,
    )
    fields.update(overrides)
    return ToolPermissionContext(**fields)


def apply_permission_update(
    update: PermissionUpdate,
    cwd: str,
    context: ToolPermissionContext,
) -> ToolPermissionContext:
    """OpenSpace ``applyPermissionUpdate`` — PermissionUpdate.ts L55-L188.

    Apply a single :class:`PermissionUpdate` to the context and return a
    new immutable context.  No disk I/O (persistence is handled by
    :func:`persist_permission_updates`).  ``cwd`` is currently unused —
    it is accepted for API symmetry with the persist family so callers
    can pass the same signature through both stages.
    """
    del cwd  # unused; kept for interface parity with the persist family

    if isinstance(update, SetModeUpdate):
        return _clone_context_with(context, mode=update.mode)

    if isinstance(update, AddRulesUpdate):
        bucket_name = _bucket_name_for_behavior(update.behavior)
        bucket: ToolPermissionRulesBySource = getattr(context, bucket_name)
        rule_strings = tuple(format_rule_value(r) for r in update.rules)
        existing = bucket.get(update.destination, ())
        new_bucket = dict(bucket)
        new_bucket[update.destination] = tuple(existing) + rule_strings
        return _clone_context_with(context, **{bucket_name: new_bucket})

    if isinstance(update, ReplaceRulesUpdate):
        bucket_name = _bucket_name_for_behavior(update.behavior)
        bucket = getattr(context, bucket_name)
        rule_strings = tuple(format_rule_value(r) for r in update.rules)
        new_bucket = dict(bucket)
        new_bucket[update.destination] = rule_strings
        return _clone_context_with(context, **{bucket_name: new_bucket})

    if isinstance(update, RemoveRulesUpdate):
        bucket_name = _bucket_name_for_behavior(update.behavior)
        bucket = getattr(context, bucket_name)
        to_remove = {format_rule_value(r) for r in update.rules}
        existing = bucket.get(update.destination, ())
        filtered = tuple(s for s in existing if s not in to_remove)
        new_bucket = dict(bucket)
        new_bucket[update.destination] = filtered
        return _clone_context_with(context, **{bucket_name: new_bucket})

    if isinstance(update, AddDirectoriesUpdate):
        new_dirs: Dict[str, AdditionalWorkingDirectory] = dict(
            context.additional_working_directories
        )
        for directory in update.directories:
            new_dirs[directory] = AdditionalWorkingDirectory(
                path=directory,
                source=update.destination,
            )
        return _clone_context_with(context, additional_working_directories=new_dirs)

    if isinstance(update, RemoveDirectoriesUpdate):
        new_dirs = dict(context.additional_working_directories)
        for directory in update.directories:
            new_dirs.pop(directory, None)
        return _clone_context_with(context, additional_working_directories=new_dirs)

    logger.warning("Unknown permission update type: %r", type(update).__name__)
    return context


# ════════════════════════════════════════════════════════════════════════
# §8  persist_permission_updates — disk writes
# ════════════════════════════════════════════════════════════════════════


def _persist_add_rules(update: AddRulesUpdate, cwd: str) -> None:
    add_permission_rules_to_settings(
        update.destination,
        list(update.rules),
        update.behavior,
        cwd,
    )


def _persist_replace_rules(update: ReplaceRulesUpdate, cwd: str) -> None:
    dest = update.destination
    if dest in ("session", "cliArg"):
        store = _store_for_source(dest)
        if store is not None:
            store.replace_rules(update.behavior, list(update.rules))
        return
    if dest not in _EDITABLE_SOURCES:
        return

    settings = get_settings_for_source(dest, cwd) or {}
    permissions_raw = settings.get("permissions")
    permissions = dict(permissions_raw) if isinstance(permissions_raw, dict) else {}
    permissions[update.behavior] = [format_rule_value(r) for r in update.rules]

    updated = dict(settings)
    updated["permissions"] = permissions
    try:
        save_settings_for_source(dest, cwd, updated)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to replace %s rules in %s: %s",
            update.behavior,
            dest,
            exc,
        )


def _persist_remove_rules(update: RemoveRulesUpdate, cwd: str) -> None:
    dest = update.destination
    if dest in ("session", "cliArg"):
        store = _store_for_source(dest)
        if store is not None:
            for rule in update.rules:
                store.remove_rule(update.behavior, rule)
        return
    if dest not in _EDITABLE_SOURCES:
        return

    settings = get_settings_for_source(dest, cwd)
    if not settings:
        return
    permissions_raw = settings.get("permissions")
    if not isinstance(permissions_raw, dict):
        return
    behavior_raw = permissions_raw.get(update.behavior)
    if not isinstance(behavior_raw, list):
        return

    to_remove = {format_rule_value(r) for r in update.rules}
    filtered: List[Any] = []
    for raw in behavior_raw:
        if isinstance(raw, str):
            canon = _normalize_rule_string(raw)
            if canon in to_remove:
                continue
        filtered.append(raw)

    permissions = dict(permissions_raw)
    permissions[update.behavior] = filtered
    updated = dict(settings)
    updated["permissions"] = permissions
    try:
        save_settings_for_source(dest, cwd, updated)
    except Exception as exc:  # pragma: no cover
        logger.warning(
            "Failed to remove %s rules from %s: %s",
            update.behavior,
            dest,
            exc,
        )


def _persist_add_directories(update: AddDirectoriesUpdate, cwd: str) -> None:
    dest = update.destination
    if dest in ("session", "cliArg"):
        store = _store_for_source(dest)
        if store is not None:
            for directory in update.directories:
                store.add_directory(directory)
        return
    if dest not in _EDITABLE_SOURCES:
        return

    settings = get_settings_for_source(dest, cwd) or {}
    permissions_raw = settings.get("permissions")
    permissions = dict(permissions_raw) if isinstance(permissions_raw, dict) else {}
    existing_raw = permissions.get("additionalDirectories")
    existing_list: List[str] = (
        [d for d in existing_raw if isinstance(d, str)]
        if isinstance(existing_raw, list)
        else []
    )

    to_add = [d for d in update.directories if d not in existing_list]
    if not to_add:
        return

    permissions["additionalDirectories"] = existing_list + to_add
    updated = dict(settings)
    updated["permissions"] = permissions
    try:
        save_settings_for_source(dest, cwd, updated)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to add directories to %s: %s", dest, exc)


def _persist_remove_directories(
    update: RemoveDirectoriesUpdate, cwd: str
) -> None:
    dest = update.destination
    if dest in ("session", "cliArg"):
        store = _store_for_source(dest)
        if store is not None:
            for directory in update.directories:
                store.remove_directory(directory)
        return
    if dest not in _EDITABLE_SOURCES:
        return

    settings = get_settings_for_source(dest, cwd)
    if not settings:
        return
    permissions_raw = settings.get("permissions")
    if not isinstance(permissions_raw, dict):
        return
    existing_raw = permissions_raw.get("additionalDirectories")
    if not isinstance(existing_raw, list):
        return

    to_remove = set(update.directories)
    filtered = [d for d in existing_raw if not (isinstance(d, str) and d in to_remove)]

    permissions = dict(permissions_raw)
    permissions["additionalDirectories"] = filtered
    updated = dict(settings)
    updated["permissions"] = permissions
    try:
        save_settings_for_source(dest, cwd, updated)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to remove directories from %s: %s", dest, exc)


def _persist_set_mode(update: SetModeUpdate, cwd: str) -> None:
    dest = update.destination
    if dest in ("session", "cliArg"):
        store = _store_for_source(dest)
        if store is not None:
            store.set_mode(update.mode)
        return
    if dest not in _EDITABLE_SOURCES:
        return

    settings = get_settings_for_source(dest, cwd) or {}
    permissions_raw = settings.get("permissions")
    permissions = dict(permissions_raw) if isinstance(permissions_raw, dict) else {}
    permissions["defaultMode"] = update.mode

    updated = dict(settings)
    updated["permissions"] = permissions
    try:
        save_settings_for_source(dest, cwd, updated)
    except Exception as exc:  # pragma: no cover
        logger.warning("Failed to persist defaultMode to %s: %s", dest, exc)


def _persist_permission_update(update: PermissionUpdate, cwd: str) -> None:
    """OpenSpace ``persistPermissionUpdate`` — PermissionUpdate.ts L222-L342."""
    if isinstance(update, AddRulesUpdate):
        _persist_add_rules(update, cwd)
    elif isinstance(update, ReplaceRulesUpdate):
        _persist_replace_rules(update, cwd)
    elif isinstance(update, RemoveRulesUpdate):
        _persist_remove_rules(update, cwd)
    elif isinstance(update, AddDirectoriesUpdate):
        _persist_add_directories(update, cwd)
    elif isinstance(update, RemoveDirectoriesUpdate):
        _persist_remove_directories(update, cwd)
    elif isinstance(update, SetModeUpdate):
        _persist_set_mode(update, cwd)
    else:  # pragma: no cover
        logger.warning(
            "Unknown permission update type: %r", type(update).__name__
        )


def persist_permission_updates(
    updates: Iterable[PermissionUpdate], cwd: str
) -> None:
    """OpenSpace ``persistPermissionUpdates`` — PermissionUpdate.ts L349-L353."""
    for update in updates:
        _persist_permission_update(update, cwd)


# ════════════════════════════════════════════════════════════════════════
# §9  load_tool_permission_context — top-level aggregator
# ════════════════════════════════════════════════════════════════════════


def _resolve_default_mode(cwd: str) -> PermissionMode:
    """Resolve effective default mode across all sources.

    Priority (later wins), matching OpenSpace's "later sources override earlier"
    convention declared in ``SETTING_SOURCES`` (constants.ts L7-L22) and
    then amended by runtime stores (cli / session):

        userSettings < projectSettings < localSettings
        < cliArg store < session store

    Returns ``"default"`` when no source specifies one.
    """
    selected: Optional[ExternalPermissionMode] = None

    for source in (
        "userSettings",
        "projectSettings",
        "localSettings",
    ):
        data = get_settings_for_source(source, cwd)
        mode = _default_mode_from_settings(data)
        if mode is not None:
            selected = mode

    # Runtime overrides (cliArg first so session wins on explicit /mode).
    for store in (_CLIARG_STORE, _SESSION_STORE):
        mode = store.get_mode()
        if mode is not None:
            selected = mode

    return selected if selected is not None else "default"


def load_tool_permission_context(
    cwd: str, mode: Optional[PermissionMode] = None
) -> ToolPermissionContext:
    """Aggregate every source into a :class:`ToolPermissionContext`.

    * Rules: merged via :func:`load_all_permission_rules_from_disk`.
    * Additional working directories: cwd (always, ``session``-sourced)
      plus any ``permissions.additionalDirectories`` found on disk plus
      runtime-store directories.  The first source to contribute a path
      wins — later sources don't overwrite the :class:`AdditionalWorkingDirectory`
      attribution.
    * Mode: explicit ``mode`` arg wins; otherwise :func:`_resolve_default_mode`.
    """
    allow, deny, ask = load_all_permission_rules_from_disk(cwd)

    working_dirs: Dict[str, AdditionalWorkingDirectory] = {
        cwd: AdditionalWorkingDirectory(path=cwd, source="session")
    }

    for source in PERMISSION_RULE_SOURCES:
        store = _store_for_source(source)
        if store is not None:
            for directory in store.get_directories():
                if directory not in working_dirs:
                    working_dirs[directory] = AdditionalWorkingDirectory(
                        path=directory, source=source
                    )
            continue

        settings = get_settings_for_source(source, cwd)
        for directory in _additional_dirs_from_settings(settings):
            if directory not in working_dirs:
                working_dirs[directory] = AdditionalWorkingDirectory(
                    path=directory, source=source
                )

    resolved_mode: PermissionMode = mode if mode is not None else _resolve_default_mode(cwd)

    return ToolPermissionContext(
        mode=resolved_mode,
        additional_working_directories=working_dirs,
        always_allow_rules=allow,
        always_deny_rules=deny,
        always_ask_rules=ask,
        # Reaching the effective bypass mode means the caller explicitly
        # selected it or opted into it as a settings default. Bash keeps a
        # separate availability guard, so carry that opt-in into the context.
        is_bypass_permissions_mode_available=(
            resolved_mode == "bypassPermissions"
        ),
    )


# ════════════════════════════════════════════════════════════════════════
# §10  Public API surface
# ════════════════════════════════════════════════════════════════════════


__all__ = [
    # Top-level loaders
    "load_tool_permission_context",
    "load_all_permission_rules_from_disk",
    "load_permissions_from_source",
    # Low-level settings I/O
    "get_settings_for_source",
    "save_settings_for_source",
    # Rule mutations (single-source, disk)
    "add_permission_rules_to_settings",
    "delete_permission_rule_from_settings",
    # Update dispatch
    "apply_permission_update",
    "persist_permission_updates",
    # Runtime stores
    "get_session_store",
    "get_cliarg_store",
    "reset_runtime_stores",
]
