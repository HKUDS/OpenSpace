"""Pinned direct-acquisition inventory for the official TB2.1 verifiers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any

VERIFIER_DEPENDENCY_CLOSURE_PATH = Path(__file__).with_name(
    "verifier_dependency_closure.json"
)


class TB21VerifierDependencyClosureError(ValueError):
    """The verifier acquisition manifest does not match its pinned task source."""


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TB21VerifierDependencyClosureError(f"{label} is not an object")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise TB21VerifierDependencyClosureError(
            f"{label} is not a list of non-empty strings"
        )
    return tuple(value)


def _profile_categories(profile: Mapping[str, Any]) -> tuple[str, ...]:
    base_fields = {"python", "uv", "pypi", "apt", "vcs", "assets"}
    if not base_fields <= set(profile) or set(profile) - base_fields not in (
        set(),
        {"candidate_project_resolution"},
    ):
        raise TB21VerifierDependencyClosureError(
            "dependency profile fields are incomplete or unknown"
        )
    python = _mapping(profile["python"], "profile.python")
    uv = _mapping(profile["uv"], "profile.uv")
    pypi = _mapping(profile["pypi"], "profile.pypi")
    apt = _mapping(profile["apt"], "profile.apt")
    vcs = profile["vcs"]
    assets = profile["assets"]
    if set(python) != {"selector", "acquisition"}:
        raise TB21VerifierDependencyClosureError("python selector fields are not exact")
    if not all(isinstance(python[name], str) and python[name] for name in python):
        raise TB21VerifierDependencyClosureError("python selector is incomplete")
    if uv.get("mode") not in {"astral-installer", "task-environment", "not-used"}:
        raise TB21VerifierDependencyClosureError("uv acquisition mode is unsupported")
    expected_uv_fields = {
        "astral-installer": {"mode", "version", "source"},
        "task-environment": {
            "mode",
            "version",
            "source_file",
            "source_digest",
        },
        "not-used": {"mode"},
    }[str(uv["mode"])]
    if set(uv) != expected_uv_fields:
        raise TB21VerifierDependencyClosureError("uv acquisition fields are not exact")
    if set(pypi) != {"indexes", "index_strategy", "requirements"}:
        raise TB21VerifierDependencyClosureError(
            "PyPI acquisition fields are not exact"
        )
    indexes = _strings(pypi["indexes"], "profile.pypi.indexes")
    requirements = _strings(pypi["requirements"], "profile.pypi.requirements")
    if not indexes or indexes[0] != "pypi-default" or not requirements:
        raise TB21VerifierDependencyClosureError("PyPI acquisition is incomplete")
    if not isinstance(pypi["index_strategy"], str) or not pypi["index_strategy"]:
        raise TB21VerifierDependencyClosureError("PyPI index strategy is absent")
    if set(apt) != {"repository", "packages"}:
        raise TB21VerifierDependencyClosureError("apt acquisition fields are not exact")
    if apt.get("repository") != "task-environment-default":
        raise TB21VerifierDependencyClosureError("apt repository semantics changed")
    apt_packages = _strings(apt["packages"], "profile.apt.packages")
    if not isinstance(vcs, list) or not isinstance(assets, list):
        raise TB21VerifierDependencyClosureError("VCS/assets acquisition is not a list")
    for item in vcs:
        entry = _mapping(item, "profile.vcs entry")
        if set(entry) != {"url", "ref", "pin_kind"} or entry.get("pin_kind") not in {
            "commit",
            "tag",
            "remote-head",
        }:
            raise TB21VerifierDependencyClosureError("VCS acquisition is incomplete")
        if any(not isinstance(entry[name], str) or not entry[name] for name in entry):
            raise TB21VerifierDependencyClosureError("VCS acquisition is incomplete")
    for item in assets:
        entry = _mapping(item, "profile.assets entry")
        if set(entry) != {"url", "ref", "pin_kind", "content_digest"}:
            raise TB21VerifierDependencyClosureError("asset acquisition is incomplete")
        if entry.get("pin_kind") not in {
            "commit",
            "tag",
            "remote-head",
            "runtime-resolved-unpinned",
        }:
            raise TB21VerifierDependencyClosureError("asset pin kind is unsupported")
        if entry.get("content_digest") is not None:
            digest = entry["content_digest"]
            if not isinstance(digest, str) or not digest.startswith("sha256:"):
                raise TB21VerifierDependencyClosureError("asset digest is malformed")
        if (
            entry.get("pin_kind") == "runtime-resolved-unpinned"
            and entry.get("content_digest") is not None
        ):
            raise TB21VerifierDependencyClosureError(
                "runtime-resolved asset must not claim a content digest"
            )

    categories = ["python", "pypi"]
    if uv["mode"] != "not-used":
        categories.append("uv")
    if apt_packages:
        categories.append("apt")
    if len(indexes) > 1:
        categories.append("additional-index")
    if vcs:
        categories.append("vcs")
    if assets:
        categories.append("assets")
    if "candidate_project_resolution" in profile:
        resolution = _mapping(
            profile["candidate_project_resolution"],
            "profile.candidate_project_resolution",
        )
        if set(resolution) != {
            "command",
            "requirements",
            "future_source",
            "agent_environment_preinstall",
            "source_file",
        } or resolution != {
            "command": [
                "uv",
                "venv",
                "--python",
                "3.13",
                ".venv",
                "&&",
                "uv",
                "pip",
                "install",
                "--python",
                ".venv/bin/python",
                "-r",
                "pyproject.toml",
            ],
            "requirements": "candidate-authored-unresolved",
            "future_source": "verifier-local-read-only-index",
            "agent_environment_preinstall": "forbidden",
            "source_file": "tasks/reshard-c4-data/tests/test_outputs.py",
        }:
            raise TB21VerifierDependencyClosureError(
                "candidate project resolution declaration is not exact"
            )
        categories.append("candidate-project-resolution")
    return tuple(categories)


def load_tb21_verifier_dependency_closure(
    *,
    repository: str,
    repository_root: Path,
    commit: str,
    tasks_tree: str,
    task_ids: Sequence[str],
    verifier_tree_oids: Mapping[str, str],
    environment_tree_oids: Mapping[str, str],
    environment_images: Mapping[str, str],
) -> Mapping[str, Any]:
    """Load and bind the package-local inventory to one official task snapshot.

    This validates authority and coverage only.  It does not install, resolve, or
    redirect dependencies, so task commands and agent-visible environments stay
    untouched.
    """

    try:
        payload = json.loads(VERIFIER_DEPENDENCY_CLOSURE_PATH.read_text("utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise TB21VerifierDependencyClosureError(
            "TB2.1 verifier dependency manifest is unavailable"
        ) from exc
    manifest = _mapping(payload, "verifier dependency manifest")
    if set(manifest) != {
        "authority",
        "scope",
        "profiles",
        "tasks",
    }:
        raise TB21VerifierDependencyClosureError("manifest fields are not exact")

    authority = _mapping(manifest["authority"], "manifest.authority")
    expected_authority = {
        "repository": repository,
        "commit": commit,
        "tasks_tree": tasks_tree,
        "task_count": len(task_ids),
    }
    if dict(authority) != expected_authority:
        raise TB21VerifierDependencyClosureError("manifest authority drifted")

    scope = _mapping(manifest["scope"], "manifest.scope")
    if (
        scope.get("environment") != "verifier-only"
        or scope.get("agent_environment") != "unchanged"
    ):
        raise TB21VerifierDependencyClosureError("verifier/agent boundary is absent")
    if scope.get("build_input_status") != (
        "static-inventory-with-candidate-project-resolution-unresolved"
    ):
        raise TB21VerifierDependencyClosureError(
            "candidate-authored dependency resolution is not disclosed"
        )
    profiles = _mapping(manifest["profiles"], "manifest.profiles")
    tasks = _mapping(manifest["tasks"], "manifest.tasks")
    expected_task_ids = tuple(sorted(task_ids))
    if tuple(sorted(tasks)) != expected_task_ids:
        raise TB21VerifierDependencyClosureError("manifest task inventory drifted")
    if set(verifier_tree_oids) != set(expected_task_ids):
        raise TB21VerifierDependencyClosureError(
            "verifier tree inventory is incomplete"
        )
    if set(environment_tree_oids) != set(expected_task_ids) or set(
        environment_images
    ) != set(expected_task_ids):
        raise TB21VerifierDependencyClosureError(
            "task environment inventory is incomplete"
        )

    referenced_profiles: set[str] = set()
    source_root = Path(repository_root).resolve(strict=True)
    for task_id in expected_task_ids:
        task = _mapping(tasks[task_id], f"manifest.tasks.{task_id}")
        if set(task) != {
            "profile",
            "categories",
            "tests_tree_git_oid",
            "environment_tree_git_oid",
            "environment_image",
            "acquisition_source_files",
        }:
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} fields are not exact"
            )
        profile_name = task.get("profile")
        if not isinstance(profile_name, str) or profile_name not in profiles:
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} has no dependency profile"
            )
        referenced_profiles.add(profile_name)
        profile = _mapping(profiles[profile_name], f"manifest.profiles.{profile_name}")
        categories = _strings(
            task["categories"], f"manifest.tasks.{task_id}.categories"
        )
        if categories != _profile_categories(profile):
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} category coverage is incomplete"
            )
        if task["tests_tree_git_oid"] != verifier_tree_oids[task_id]:
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} verifier tree drifted"
            )
        if (
            task["environment_tree_git_oid"] != environment_tree_oids[task_id]
            or task["environment_image"] != environment_images[task_id]
        ):
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} environment binding drifted"
            )
        sources = _mapping(
            task["acquisition_source_files"],
            f"manifest.tasks.{task_id}.acquisition_source_files",
        )
        expected_test_sh = f"tasks/{task_id}/tests/test.sh"
        if expected_test_sh not in sources or any(
            not isinstance(path, str)
            or not path.startswith(f"tasks/{task_id}/tests/")
            or not isinstance(digest, str)
            or not digest.startswith("sha256:")
            for path, digest in sources.items()
        ):
            raise TB21VerifierDependencyClosureError(
                f"manifest task {task_id!r} acquisition sources are incomplete"
            )
        for source, expected_digest in sources.items():
            try:
                source_path = (source_root / source).resolve(strict=True)
                source_path.relative_to(source_root / "tasks" / task_id / "tests")
            except (OSError, ValueError) as exc:
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} acquisition source is unavailable"
                ) from exc
            observed_digest = (
                "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
            )
            if observed_digest != expected_digest:
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} acquisition source drifted"
                )
        uv = _mapping(profile["uv"], f"manifest.tasks.{task_id}.uv")
        if uv["mode"] == "task-environment":
            source = uv["source_file"]
            expected_digest = uv["source_digest"]
            if not isinstance(source, str) or not isinstance(expected_digest, str):
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} uv provenance is incomplete"
                )
            try:
                source_path = (source_root / source).resolve(strict=True)
                source_path.relative_to(source_root / "tasks" / task_id / "environment")
            except (OSError, ValueError) as exc:
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} uv provenance is unavailable"
                ) from exc
            observed_digest = (
                "sha256:" + hashlib.sha256(source_path.read_bytes()).hexdigest()
            )
            if observed_digest != expected_digest:
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} uv provenance drifted"
                )
        if "candidate_project_resolution" in profile:
            source = profile["candidate_project_resolution"]["source_file"]
            if source not in sources:
                raise TB21VerifierDependencyClosureError(
                    f"manifest task {task_id!r} dynamic resolution source is absent"
                )
    if referenced_profiles != set(profiles):
        raise TB21VerifierDependencyClosureError("manifest contains unused profiles")
    return MappingProxyType(dict(manifest))


__all__ = [
    "VERIFIER_DEPENDENCY_CLOSURE_PATH",
    "TB21VerifierDependencyClosureError",
    "load_tb21_verifier_dependency_closure",
]
