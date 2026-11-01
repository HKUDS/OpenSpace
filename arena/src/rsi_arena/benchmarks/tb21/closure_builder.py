"""Build TB2.1 verifier-only images from task source and content locks.

The online ``lock`` phase resolves and fetches content.  ``build`` and
``verify`` consume only that local content and run Docker with networking
disabled.  This module is deliberately TB2.1-specific; it is not a generic
artifact or image-build framework.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from email.parser import Parser
from pathlib import Path, PurePosixPath
from types import MappingProxyType, SimpleNamespace
from typing import Any

import tomllib

from rsi_arena.benchmarks.harbor_runtime.materials import task_tree_digest

from .offline_verifier import (
    AUTHORITY_SCHEMA,
    OfflineClosureAuthority,
    OfflineTaskBinding,
    TB21OfflineVerifier,
    TB21OfflineVerifierError,
    canonical_app_identity,
    decode_offline_closure_authority,
    load_offline_closure_authority,
    offline_verifier_launch_kwargs,
)
from .tasks import load_tb21_task_snapshot
from .verifier_dependencies import VERIFIER_DEPENDENCY_CLOSURE_PATH

LOCK_SCHEMA = "tb21-offline-verifier-closure-lock"
SUPPORTED_PLATFORM = "linux/amd64"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TASK_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_PIN_RE = re.compile(r"([A-Za-z0-9_.-]+)==([^;\s]+)$")

PYTORCH_CPU_INDEX = "https://download.pytorch.org/whl/cpu"
MNIST_MIRROR = "https://ossci-datasets.s3.amazonaws.com/mnist/"
MNIST_RESOURCES = (
    ("train-images-idx3-ubyte.gz", "f68b3c2dcbeaaa9fbdd348bbdeb94873"),
    ("train-labels-idx1-ubyte.gz", "d53e105ee54ea40749a09fcbcd1e9432"),
    ("t10k-images-idx3-ubyte.gz", "9fb629c4189551a2d022fa330f9573f3"),
    ("t10k-labels-idx1-ubyte.gz", "ec29112dd5afa0611ce80d1b7f02629c"),
)
C4_REPOSITORY_URL = "https://huggingface.co/datasets/allenai/c4"
C4_SHARD = "en/c4-train.00009-of-01024.json.gz"

# These task-specific acquisitions come from the frozen dependency census.
# They are public source recipes, not historical image identities.
_FILTER_JS_REPOSITORY = "https://github.com/davidwagner/html-sanitizer-testbed.git"
_FILTER_JS_URL = (
    "https://github.com/davidwagner/html-sanitizer-testbed/"
    "archive/refs/heads/master.zip"
)
_SAM_REPOSITORY = "https://github.com/ChaoningZhang/MobileSAM.git"
_SAM_WEIGHT_URL = (
    "https://github.com/ChaoningZhang/MobileSAM/raw/master/weights/mobile_sam.pt"
)
_PINNED_PYPI_SDISTS = MappingProxyType(
    {
        "mailman": MappingProxyType(
            {
                "requirement": "mailman==3.3.8",
                "filename": "mailman-3.3.8.tar.gz",
                "url": (
                    "https://files.pythonhosted.org/packages/96/b1/"
                    "4863d865cc76c981e584c6de99d57697135b587bdcf36a436b6fccea3817/"
                    "mailman-3.3.8.tar.gz"
                ),
                "sha256": (
                    "sha256:83ac07ee55ea2b4c89f00c4ed4717132f06270127d3465a53de3c5cb197d41ce"
                ),
            }
        ),
        "regex-chess": MappingProxyType(
            {
                "requirement": "chess==1.11.2",
                "filename": "chess-1.11.2.tar.gz",
                "url": (
                    "https://files.pythonhosted.org/packages/93/09/"
                    "7d04d7581ae3bb8b598017941781bceb7959dd1b13e3ebf7b6a2cd843bc9/"
                    "chess-1.11.2.tar.gz"
                ),
                "sha256": (
                    "sha256:a8b43e5678fdb3000695bdaa573117ad683761e5ca38e591c4826eba6d25bb39"
                ),
            }
        ),
    }
)


class ClosureBuilderError(RuntimeError):
    """One actionable task/profile/artifact build failure."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_digest(value: Mapping[str, object]) -> str:
    try:
        payload = json.dumps(
            dict(value),
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ClosureBuilderError("canonical digest input is invalid") from exc
    return _sha256_bytes(payload)


def _atomic_json(path: Path, value: Mapping[str, object]) -> None:
    path.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(
        json.dumps(dict(value), indent=2, sort_keys=True) + "\n", "utf-8"
    )
    os.replace(temporary, path)


def _run(
    command: Sequence[str],
    *,
    timeout: float = 3600.0,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(
            tuple(command),
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClosureBuilderError(f"command failed to run: {command[0]}") from exc
    if check and completed.returncode != 0:
        detail = completed.stdout.strip()[-4000:]
        raise ClosureBuilderError(
            f"command exited {completed.returncode}: {' '.join(command[:4])}\n{detail}"
        )
    return completed


def _docker(docker: str, docker_host: str, *arguments: str) -> tuple[str, ...]:
    return (docker, "--host", docker_host, *arguments)


def _run_task_artifact(
    command: Sequence[str],
    *,
    task_id: str,
    profile: str,
    artifact: str,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    try:
        return _run(command, timeout=timeout)
    except ClosureBuilderError as exc:
        raise ClosureBuilderError(f"{task_id}/{profile}/{artifact}: {exc}") from exc


def _require_output_root(path: Path) -> Path:
    root = path.expanduser().absolute()
    if root == Path("/"):
        raise ClosureBuilderError("output directory must be narrow")
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    return root


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureBuilderError(f"JSON input is unavailable: {path}") from exc
    if not isinstance(value, dict):
        raise ClosureBuilderError(f"JSON input must be an object: {path}")
    return value


def _write_state(root: Path, *, phase: str, task_ids: Sequence[str]) -> None:
    _atomic_json(
        root / "closure-builder-state.json",
        {
            "phase": phase,
            "task_ids": sorted(task_ids),
            "updated_at": _utc_now(),
        },
    )


def _git(repository: Path, *arguments: str) -> str:
    return _run(
        ("git", "-C", str(repository), *arguments), timeout=120.0
    ).stdout.strip()


def _task_manifest(repository: Path, task_id: str) -> Mapping[str, object]:
    path = repository / "tasks" / task_id / "task.toml"
    try:
        value = tomllib.loads(path.read_text("utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise ClosureBuilderError(f"{task_id}: task.toml is invalid") from exc
    if not isinstance(value, dict):
        raise ClosureBuilderError(f"{task_id}: task.toml is not an object")
    return value


def _select_tasks(
    census: Mapping[str, object],
    requested_tasks: Sequence[str],
    requested_families: Sequence[str],
) -> tuple[str, ...]:
    raw_tasks = census.get("tasks")
    if not isinstance(raw_tasks, Mapping):
        raise ClosureBuilderError("dependency census has no task inventory")
    unknown = sorted(set(requested_tasks) - set(raw_tasks))
    if unknown:
        raise ClosureBuilderError(f"unknown TB2.1 task: {unknown[0]}")
    selected = set(requested_tasks)
    for family in requested_families:
        matches: set[str] = set()
        for task_id, value in raw_tasks.items():
            if not isinstance(value, Mapping):
                continue
            profile_name = value.get("profile")
            profiles = census.get("profiles")
            profile = (
                profiles.get(profile_name)
                if isinstance(profiles, Mapping) and isinstance(profile_name, str)
                else None
            )
            if profile_name == family or (
                isinstance(profile, Mapping)
                and _profile_family(str(task_id), str(profile_name), profile) == family
            ):
                matches.add(str(task_id))
        if not matches:
            raise ClosureBuilderError(f"unknown TB2.1 dependency family: {family}")
        selected.update(matches)
    if not requested_tasks and not requested_families:
        selected.update(str(task_id) for task_id in raw_tasks)
    if not selected:
        raise ClosureBuilderError("no TB2.1 tasks selected")
    return tuple(sorted(selected))


def _profile_for_task(
    census: Mapping[str, object], task_id: str
) -> tuple[str, Mapping[str, object], Mapping[str, object]]:
    tasks = census["tasks"]
    profiles = census["profiles"]
    assert isinstance(tasks, Mapping) and isinstance(profiles, Mapping)
    task = tasks[task_id]
    if not isinstance(task, Mapping):
        raise ClosureBuilderError(f"{task_id}: dependency census entry is invalid")
    profile_name = task.get("profile")
    profile = profiles.get(profile_name) if isinstance(profile_name, str) else None
    if not isinstance(profile_name, str) or not isinstance(profile, Mapping):
        raise ClosureBuilderError(f"{task_id}: dependency profile is unavailable")
    return profile_name, task, profile


def _wheel_only_requirements(
    task_id: str, profile_name: str, profile: Mapping[str, object]
) -> tuple[str, ...] | None:
    apt = profile.get("apt")
    pypi = profile.get("pypi")
    python = profile.get("python")
    uv = profile.get("uv")
    if not all(isinstance(value, Mapping) for value in (apt, pypi, python, uv)):
        raise ClosureBuilderError(f"{task_id}/{profile_name}: profile is malformed")
    assert isinstance(apt, Mapping)
    assert isinstance(pypi, Mapping)
    assert isinstance(python, Mapping)
    assert isinstance(uv, Mapping)
    unsupported: list[str] = []
    if apt.get("packages") != []:
        unsupported.append("apt")
    if profile.get("assets") != []:
        unsupported.append("assets")
    if profile.get("vcs") != []:
        unsupported.append("vcs")
    if python.get("acquisition") != "task-environment":
        unsupported.append("python")
    if uv.get("mode") != "not-used":
        unsupported.append("uv")
    requirements = pypi.get("requirements")
    if not isinstance(requirements, list) or not requirements:
        unsupported.append("pypi")
        requirements = []
    if any(
        not isinstance(item, str) or _PIN_RE.fullmatch(item) is None
        for item in requirements
    ):
        unsupported.append("unpinned-pypi")
    if unsupported:
        return None
    return tuple(str(item) for item in requirements)


def _profile_sequence(
    profile: Mapping[str, object], key: str, task_id: str, profile_name: str
) -> tuple[object, ...]:
    value = profile.get(key)
    if not isinstance(value, list):
        raise ClosureBuilderError(f"{task_id}/{profile_name}: {key} is malformed")
    return tuple(value)


def _profile_family(
    task_id: str, profile_name: str, profile: Mapping[str, object]
) -> str:
    """Return the public dependency family without carrying old image IDs."""

    apt = profile.get("apt")
    python = profile.get("python")
    uv = profile.get("uv")
    pypi = profile.get("pypi")
    if not all(isinstance(value, Mapping) for value in (apt, python, uv, pypi)):
        raise ClosureBuilderError(f"{task_id}/{profile_name}: profile is malformed")
    assert isinstance(apt, Mapping)
    assert isinstance(python, Mapping)
    assert isinstance(uv, Mapping)
    assert isinstance(pypi, Mapping)
    packages = _profile_sequence(apt, "packages", task_id, profile_name)
    requirements = _profile_sequence(pypi, "requirements", task_id, profile_name)
    indexes = _profile_sequence(pypi, "indexes", task_id, profile_name)
    vcs = _profile_sequence(profile, "vcs", task_id, profile_name)
    assets = _profile_sequence(profile, "assets", task_id, profile_name)
    if apt.get("repository") != "task-environment-default":
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: unsupported apt repository"
        )
    if pypi.get("index_strategy") not in {"first-index", "unsafe-best-match"}:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: unsupported PyPI index strategy"
        )
    if not all(isinstance(item, str) for item in packages + requirements + indexes):
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: profile strings malformed"
        )
    if task_id == "reshard-c4-data":
        return "c4-candidate-uv"
    if task_id == "pytorch-model-cli":
        return "mnist-cpu-torch"
    if vcs or assets:
        return "vcs-assets"
    if any(str(item).startswith(("torch==", "torchvision==")) for item in requirements):
        return (
            "cpu-index-torch" if PYTORCH_CPU_INDEX in indexes else "default-index-torch"
        )
    if python.get("selector") != "3.13" or uv.get("mode") != "astral-installer":
        return "special-python"
    if packages not in {(), ("curl",)}:
        return "apt"
    return "core-scientific"


def _download(
    url: str,
    destination: Path,
    *,
    task_id: str,
    profile_name: str,
    timeout: float = 900.0,
) -> Mapping[str, object]:
    destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            resolved_parts = urllib.parse.urlsplit(response.geturl())
            resolved = urllib.parse.urlunsplit(
                (
                    resolved_parts.scheme,
                    resolved_parts.netloc,
                    resolved_parts.path,
                    "",
                    "",
                )
            )
            with destination.open("xb") as output:
                while True:
                    chunk = response.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
                    digest.update(chunk)
                    size += len(chunk)
    except (OSError, urllib.error.URLError) as exc:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}/asset:{destination.name}: download failed"
        ) from exc
    if size <= 0:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}/asset:{destination.name}: empty download"
        )
    return MappingProxyType(
        {
            "requested_url": url,
            "resolved_url": resolved,
            "path": destination.name,
            "sha256": "sha256:" + digest.hexdigest(),
            "size": size,
        }
    )


def _git_remote_commit(url: str, ref: str, *, label: str) -> str:
    output = _run(("git", "ls-remote", url, ref), timeout=300.0).stdout.splitlines()
    matches = [line.split() for line in output if line.strip()]
    if len(matches) != 1 or len(matches[0]) != 2:
        raise ClosureBuilderError(f"{label}: remote ref did not resolve once")
    commit, observed_ref = matches[0]
    if observed_ref != ref or re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ClosureBuilderError(f"{label}: remote ref did not resolve immutably")
    return commit


def _git_bundle(
    *,
    url: str,
    ref: str,
    pin_kind: str,
    destination: Path,
    task_id: str,
    profile_name: str,
) -> Mapping[str, object]:
    label = f"{task_id}/{profile_name}/vcs:{url}"
    with tempfile.TemporaryDirectory(prefix="tb21-closure-vcs-") as temporary:
        repository = Path(temporary) / "source.git"
        _run(("git", "clone", "--mirror", url, str(repository)), timeout=1800.0)
        if pin_kind == "tag":
            selector = f"refs/tags/{ref}^{{commit}}"
        elif pin_kind == "commit":
            selector = f"{ref}^{{commit}}"
        elif pin_kind == "remote-head":
            if ref == "remote-default-head":
                selector = "HEAD^{commit}"
            else:
                selector = f"{ref}^{{commit}}"
        else:
            raise ClosureBuilderError(f"{label}: unsupported pin kind {pin_kind}")
        commit = _run(
            ("git", f"--git-dir={repository}", "rev-parse", selector), timeout=120.0
        ).stdout.strip()
        tree = _run(
            ("git", f"--git-dir={repository}", "rev-parse", f"{commit}^{{tree}}"),
            timeout=120.0,
        ).stdout.strip()
        if not all(re.fullmatch(r"[0-9a-f]{40}", value) for value in (commit, tree)):
            raise ClosureBuilderError(f"{label}: source identity is malformed")
        destination.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
        _run(
            (
                "git",
                f"--git-dir={repository}",
                "bundle",
                "create",
                str(destination),
                "--all",
            ),
            timeout=1800.0,
        )
        _run(
            ("git", "-C", str(repository), "bundle", "verify", str(destination)),
            timeout=300.0,
        )
    return MappingProxyType(
        {
            "kind": "git-bundle",
            "url": url,
            "requested_ref": ref,
            "pin_kind": pin_kind,
            "resolved_commit": commit,
            "resolved_tree": tree,
            "path": destination.name,
            "sha256": _sha256_file(destination),
            "size": destination.stat().st_size,
        }
    )


def _write_filter_hook(path: Path, archive: str) -> None:
    path.write_text(
        "\n".join(
            (
                "import os, shutil, tempfile, urllib.request",
                f"_URL = {_FILTER_JS_URL!r}",
                f"_ARCHIVE = {archive!r}",
                "_original = urllib.request.urlretrieve",
                "def _locked(url, filename=None, reporthook=None, data=None):",
                "    if url != _URL:",
                "        return _original(url, filename, reporthook, data)",
                "    if filename is None:",
                "        fd, filename = tempfile.mkstemp(suffix='.zip'); os.close(fd)",
                "    shutil.copyfile(_ARCHIVE, filename)",
                "    if reporthook:",
                "        size = os.path.getsize(filename); reporthook(1, size, size)",
                "    return filename, None",
                "urllib.request.urlretrieve = _locked",
                "",
            )
        ),
        "utf-8",
    )


def _write_c4_hook(path: Path, shard: str) -> None:
    path.write_text(
        "\n".join(
            (
                "import datasets",
                f"_SHARD = {shard!r}",
                "_original = datasets.load_dataset",
                "def _locked(path, *args, **kwargs):",
                "    expected = {'train': ['en/c4-train.00009-of-01024.json.gz']}",
                "    if path == 'allenai/c4' and kwargs.get('data_files') == expected:",
                "        changed = dict(kwargs); changed['data_files'] = {'train': [_SHARD]}",
                "        return _original('json', *args, **changed)",
                "    return _original(path, *args, **kwargs)",
                "datasets.load_dataset = _locked",
                "",
            )
        ),
        "utf-8",
    )


def _write_mnist_hook(path: Path, source: str) -> None:
    path.write_text(
        "\n".join(
            (
                "import pathlib, shutil",
                "from torchvision.datasets import MNIST",
                f"_SOURCE = pathlib.Path({source!r})",
                "_original = MNIST.__init__",
                "def _locked(self, root, *args, **kwargs):",
                "    raw = pathlib.Path(root) / 'MNIST' / 'raw'",
                "    raw.mkdir(parents=True, exist_ok=True)",
                "    for item in _SOURCE.iterdir():",
                "        if item.is_file(): shutil.copyfile(item, raw / item.name)",
                "    changed = dict(kwargs); changed['download'] = False",
                "    return _original(self, root, *args, **changed)",
                "MNIST.__init__ = _locked",
                "",
            )
        ),
        "utf-8",
    )


def _resolve_recipe_inputs(
    *,
    task_id: str,
    profile_name: str,
    profile: Mapping[str, object],
    root: Path,
) -> tuple[Mapping[str, object], ...]:
    root.mkdir(mode=0o755, parents=True, exist_ok=True)
    records: list[Mapping[str, object]] = []
    raw_vcs = _profile_sequence(profile, "vcs", task_id, profile_name)
    raw_assets = _profile_sequence(profile, "assets", task_id, profile_name)
    if raw_assets and task_id not in {
        "filter-js-from-html",
        "pytorch-model-cli",
        "reshard-c4-data",
        "sam-cell-seg",
    }:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: asset recipe has no public resolver"
        )
    for index, raw in enumerate(raw_vcs):
        if not isinstance(raw, Mapping):
            raise ClosureBuilderError(f"{task_id}/{profile_name}: VCS entry malformed")
        url, ref, pin_kind = raw.get("url"), raw.get("ref"), raw.get("pin_kind")
        if not all(isinstance(value, str) and value for value in (url, ref, pin_kind)):
            raise ClosureBuilderError(f"{task_id}/{profile_name}: VCS identity absent")
        records.append(
            _git_bundle(
                url=str(url),
                ref=str(ref),
                pin_kind=str(pin_kind),
                destination=root / f"vcs-{index}.bundle",
                task_id=task_id,
                profile_name=profile_name,
            )
        )

    if task_id == "filter-js-from-html":
        with tempfile.TemporaryDirectory(prefix="tb21-filter-js-") as temporary:
            repository = Path(temporary) / "source.git"
            _run(
                ("git", "clone", "--mirror", _FILTER_JS_REPOSITORY, str(repository)),
                timeout=1800.0,
            )
            commit = _run(
                (
                    "git",
                    f"--git-dir={repository}",
                    "rev-parse",
                    "refs/heads/master^{commit}",
                ),
                timeout=120.0,
            ).stdout.strip()
            tree = _run(
                ("git", f"--git-dir={repository}", "rev-parse", f"{commit}^{{tree}}"),
                timeout=120.0,
            ).stdout.strip()
            archive = root / "html-sanitizer-testbed-master.zip"
            _run(
                (
                    "git",
                    f"--git-dir={repository}",
                    "archive",
                    "--format=zip",
                    "--prefix=html-sanitizer-testbed-master/",
                    f"--output={archive}",
                    commit,
                ),
                timeout=300.0,
            )
        hook = root / "sitecustomize.py"
        _write_filter_hook(hook, "/opt/rsi-arena/tb21/assets/filter-js.zip")
        records.extend(
            (
                MappingProxyType(
                    {
                        "kind": "filter-js-archive",
                        "url": _FILTER_JS_URL,
                        "resolved_commit": commit,
                        "resolved_tree": tree,
                        "path": archive.name,
                        "sha256": _sha256_file(archive),
                        "size": archive.stat().st_size,
                    }
                ),
                MappingProxyType(
                    {
                        "kind": "python-hook",
                        "path": hook.name,
                        "sha256": _sha256_file(hook),
                        "size": hook.stat().st_size,
                    }
                ),
            )
        )
    elif task_id == "sam-cell-seg":
        commit = _git_remote_commit(
            _SAM_REPOSITORY, "refs/heads/master", label="sam-cell-seg/weight"
        )
        weight = root / "mobile_sam.pt"
        record = dict(
            _download(
                "https://raw.githubusercontent.com/ChaoningZhang/MobileSAM/"
                + commit
                + "/weights/mobile_sam.pt",
                weight,
                task_id=task_id,
                profile_name=profile_name,
            )
        )
        record.update(
            {
                "kind": "sam-weight",
                "original_url": _SAM_WEIGHT_URL,
                "resolved_commit": commit,
            }
        )
        wrapper = root / "curl"
        wrapper.write_text(
            "#!/bin/sh\n"
            'if [ "$#" -eq 4 ] && [ "$1" = -L ] && [ "$2" = -o ] '
            f'&& [ "$4" = {shlex.quote(_SAM_WEIGHT_URL)} ]; then\n'
            '  cp /opt/rsi-arena/tb21/assets/mobile_sam.pt "$3"\n  exit 0\nfi\n'
            'exec /usr/bin/curl "$@"\n',
            "utf-8",
        )
        wrapper.chmod(0o755)
        records.extend(
            (
                MappingProxyType(record),
                MappingProxyType(
                    {
                        "kind": "curl-wrapper",
                        "path": wrapper.name,
                        "sha256": _sha256_file(wrapper),
                        "size": wrapper.stat().st_size,
                    }
                ),
            )
        )
    elif task_id == "pytorch-model-cli":
        resources: list[Mapping[str, object]] = []
        for filename, expected_md5 in MNIST_RESOURCES:
            record = dict(
                _download(
                    MNIST_MIRROR + filename,
                    root / filename,
                    task_id=task_id,
                    profile_name=profile_name,
                    timeout=300.0,
                )
            )
            content = (root / filename).read_bytes()
            md5 = hashlib.md5(content, usedforsecurity=False).hexdigest()
            if md5 != expected_md5:
                raise ClosureBuilderError(
                    f"{task_id}/{profile_name}/{filename}: upstream checksum changed"
                )
            try:
                extracted = gzip.decompress(content)
            except (OSError, EOFError) as exc:
                raise ClosureBuilderError(
                    f"{task_id}/{profile_name}/{filename}: invalid gzip"
                ) from exc
            record.update(
                {
                    "kind": "mnist-resource",
                    "upstream_md5": expected_md5,
                    "extracted_sha256": _sha256_bytes(extracted),
                    "extracted_size": len(extracted),
                }
            )
            resources.append(MappingProxyType(record))
        records.extend(resources)
        hook = root / "sitecustomize.py"
        _write_mnist_hook(hook, "/opt/rsi-arena/tb21/assets/mnist/raw")
        records.append(
            MappingProxyType(
                {
                    "kind": "python-hook",
                    "path": hook.name,
                    "sha256": _sha256_file(hook),
                    "size": hook.stat().st_size,
                }
            )
        )
    elif task_id == "reshard-c4-data":
        commit = _git_remote_commit(
            C4_REPOSITORY_URL, "refs/heads/main", label="reshard-c4-data/C4"
        )
        shard = root / Path(C4_SHARD).name
        record = dict(
            _download(
                f"{C4_REPOSITORY_URL}/resolve/{commit}/{C4_SHARD}?download=true",
                shard,
                task_id=task_id,
                profile_name=profile_name,
                timeout=1800.0,
            )
        )
        record.update(
            {
                "kind": "c4-shard",
                "dataset": "allenai/c4",
                "resolved_commit": commit,
                "data_file": C4_SHARD,
            }
        )
        hook = root / "sitecustomize.py"
        _write_c4_hook(hook, "/opt/rsi-arena/tb21/assets/" + shard.name)
        records.extend(
            (
                MappingProxyType(record),
                MappingProxyType(
                    {
                        "kind": "python-hook",
                        "path": hook.name,
                        "sha256": _sha256_file(hook),
                        "size": hook.stat().st_size,
                    }
                ),
            )
        )
    pinned_sdist = _PINNED_PYPI_SDISTS.get(task_id)
    if pinned_sdist is not None:
        sdist = root / str(pinned_sdist["filename"])
        record = dict(
            _download(
                str(pinned_sdist["url"]),
                sdist,
                task_id=task_id,
                profile_name=profile_name,
            )
        )
        if record["sha256"] != pinned_sdist["sha256"]:
            raise ClosureBuilderError(f"{task_id}: PyPI source digest changed")
        requirement = str(pinned_sdist["requirement"])
        match = _PIN_RE.fullmatch(requirement)
        assert match is not None
        record.update(
            {
                "kind": "pypi-sdist",
                "requirement": requirement,
                "project": match.group(1),
                "version": match.group(2),
            }
        )
        records.append(MappingProxyType(record))
    return tuple(records)


def _render_online_resolver_dockerfile(
    *,
    base_tag: str,
    task_id: str,
    profile_name: str,
    profile: Mapping[str, object],
    family: str,
    acquisitions: Sequence[Mapping[str, object]],
) -> str:
    apt = profile["apt"]
    python = profile["python"]
    uv = profile["uv"]
    pypi = profile["pypi"]
    assert all(isinstance(value, Mapping) for value in (apt, python, uv, pypi))
    assert isinstance(apt, Mapping)
    assert isinstance(python, Mapping)
    assert isinstance(uv, Mapping)
    assert isinstance(pypi, Mapping)
    packages = tuple(str(value) for value in apt["packages"])
    requirements = tuple(str(value) for value in pypi["requirements"])
    indexes = tuple(str(value) for value in pypi["indexes"])
    lines = [
        f"FROM {base_tag}",
        "USER root",
        'SHELL ["/bin/bash", "-o", "pipefail", "-c"]',
    ]
    if packages:
        lines.append(
            "RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y "
            + shlex.join(packages)
            + " && rm -rf /var/lib/apt/lists/*"
        )

    bundle_records = [item for item in acquisitions if item.get("kind") == "git-bundle"]
    if bundle_records:
        lines.append("RUN mkdir -p /opt/rsi-arena/tb21/vcs")
        for index, record in enumerate(bundle_records):
            filename = str(record["path"])
            target = f"/opt/rsi-arena/tb21/vcs/source-{index}.bundle"
            mirror = f"/opt/rsi-arena/tb21/vcs/source-{index}.git"
            lines.append(f"COPY inputs/{filename} {target}")
            lines.append(
                "RUN echo "
                + shlex.quote(str(record["sha256"])[7:] + "  " + target)
                + " | sha256sum -c - && git clone --mirror "
                + shlex.quote(target)
                + " "
                + shlex.quote(mirror)
                + " && git config --system protocol.file.allow always"
                + " && git config --system url."
                + shlex.quote("file://" + mirror)
                + ".insteadOf "
                + shlex.quote(str(record["url"]))
            )

    sdist_records = [item for item in acquisitions if item.get("kind") == "pypi-sdist"]
    if sdist_records:
        lines.append("RUN mkdir -p /opt/rsi-arena/tb21/sdists")
        for record in sdist_records:
            filename = str(record["path"])
            target = "/opt/rsi-arena/tb21/sdists/" + filename
            lines.append(f"COPY inputs/{filename} {target}")
            lines.append(
                "RUN echo "
                + shlex.quote(str(record["sha256"])[7:] + "  " + target)
                + " | sha256sum -c -"
            )

    if task_id == "filter-js-from-html":
        archive = next(
            item for item in acquisitions if item.get("kind") == "filter-js-archive"
        )
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{archive['path']} /opt/rsi-arena/tb21/assets/filter-js.zip",
                f"COPY inputs/{hook['path']} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
            )
        )
    elif task_id == "sam-cell-seg":
        weight = next(item for item in acquisitions if item.get("kind") == "sam-weight")
        wrapper = next(
            item for item in acquisitions if item.get("kind") == "curl-wrapper"
        )
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/bin",
                f"COPY inputs/{weight['path']} /opt/rsi-arena/tb21/assets/mobile_sam.pt",
                f"COPY inputs/{wrapper['path']} /opt/rsi-arena/tb21/bin/curl",
                "RUN chmod 0755 /opt/rsi-arena/tb21/bin/curl",
                'ENV PATH="/opt/rsi-arena/tb21/bin:/root/.local/bin:${PATH}"',
            )
        )
    elif task_id == "pytorch-model-cli":
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets/mnist/raw /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{hook['path']} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
            )
        )
        for record in acquisitions:
            if record.get("kind") != "mnist-resource":
                continue
            filename = str(record["path"])
            destination = "/opt/rsi-arena/tb21/assets/mnist/raw/" + filename
            lines.append(f"COPY inputs/{filename} {destination}")
            script = (
                "import gzip,hashlib,pathlib;"
                f"p=pathlib.Path({destination!r});d=gzip.decompress(p.read_bytes());"
                f"assert hashlib.sha256(d).hexdigest()=={str(record['extracted_sha256'])[7:]!r};"
                "p.with_suffix('').write_bytes(d)"
            )
            lines.append("RUN python -c " + shlex.quote(script))
    elif task_id == "reshard-c4-data":
        shard = next(item for item in acquisitions if item.get("kind") == "c4-shard")
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{shard['path']} /opt/rsi-arena/tb21/assets/{shard['path']}",
                f"COPY inputs/{hook['path']} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
                "ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1",
            )
        )

    uv_mode = uv.get("mode")
    if uv_mode == "astral-installer":
        source = uv.get("source")
        version = uv.get("version")
        if source != f"https://astral.sh/uv/{version}/install.sh":
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: uv installer source drifted"
            )
        lines.extend(
            (
                "RUN curl -LsSf " + shlex.quote(str(source)) + " | sh",
                'ENV PATH="/root/.local/bin:${PATH}"',
            )
        )
    elif uv_mode not in {"not-used", "task-environment"}:
        raise ClosureBuilderError(f"{task_id}/{profile_name}: unsupported uv mode")

    if uv_mode == "not-used":
        lines.append(
            "RUN python -m pip install "
            + shlex.join(requirements)
            + " && python -m pip check"
        )
    else:
        warm = ["uvx"]
        for index in indexes:
            if index == "pypi-default":
                continue
            warm.extend(("--index", index))
        if pypi.get("index_strategy") == "unsafe-best-match":
            warm.extend(("--index-strategy", "unsafe-best-match"))
        warm.extend(("-p", str(python.get("selector"))))
        locked_sdists = {
            str(record["requirement"]): (
                "/opt/rsi-arena/tb21/sdists/" + str(record["path"])
            )
            for record in sdist_records
        }
        for requirement in requirements:
            warm.extend(("-w", locked_sdists.get(requirement, requirement)))
        warm.extend(("pytest", "--version"))
        lines.append("RUN " + shlex.join(warm))
    lines.extend(
        (
            "ENV UV_OFFLINE=1",
            f'LABEL org.rsi_arena.tb21.task="{task_id}"',
            f'LABEL org.rsi_arena.tb21.profile="{profile_name}"',
            f'LABEL org.rsi_arena.tb21.family="{family}"',
            'LABEL org.rsi_arena.tb21.role="resolved-verifier-input"',
            "",
        )
    )
    return "\n".join(lines)


def _inspect_image(
    docker: str, docker_host: str, reference: str
) -> Mapping[str, object]:
    result = _run(
        _docker(docker, docker_host, "image", "inspect", reference), timeout=120.0
    )
    try:
        values = json.loads(result.stdout)
        value = values[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        raise ClosureBuilderError(f"Docker image inspect failed: {reference}") from exc
    if not isinstance(value, dict):
        raise ClosureBuilderError(f"Docker image inspect is malformed: {reference}")
    image_id = value.get("Id")
    if not isinstance(image_id, str) or _SHA256_RE.fullmatch(image_id) is None:
        raise ClosureBuilderError(f"Docker image has no content ID: {reference}")
    return value


def _wheel_metadata(path: Path) -> Mapping[str, object]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = [
                name
                for name in archive.namelist()
                if name.endswith(".dist-info/METADATA") and name.count("/") == 1
            ]
            if len(names) != 1:
                raise ClosureBuilderError(f"wheel metadata is ambiguous: {path.name}")
            message = Parser().parsestr(archive.read(names[0]).decode("utf-8"))
    except (OSError, UnicodeError, zipfile.BadZipFile) as exc:
        raise ClosureBuilderError(f"wheel is unreadable: {path.name}") from exc
    name = message.get("Name")
    version = message.get("Version")
    if not name or not version:
        raise ClosureBuilderError(f"wheel identity is absent: {path.name}")
    return MappingProxyType(
        {
            "filename": path.name,
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
            "name": name,
            "version": version,
            "requires_dist": sorted(message.get_all("Requires-Dist") or []),
        }
    )


def _resolved_runtime_inventory(
    *,
    docker: str,
    docker_host: str,
    image_id: str,
    selector: str,
    task_id: str,
    profile_name: str,
) -> Mapping[str, object]:
    script = "\n".join(
        (
            "import hashlib,json,pathlib,shutil,subprocess",
            "def identity(path):",
            " p=pathlib.Path(path).resolve(); return {'path':str(p),'sha256':'sha256:'+hashlib.sha256(p.read_bytes()).hexdigest(),'size':p.stat().st_size}",
            "uv=shutil.which('uv')",
            f"selector={selector!r}",
            "if selector == 'environment-default': py=shutil.which('python') or shutil.which('python3')",
            "else: py=subprocess.run(['uv','python','find',selector],check=True,capture_output=True,text=True).stdout.strip()",
            "apt=subprocess.run(['dpkg-query','-W','-f=${Package}=${Version}\\n'],check=False,capture_output=True,text=True).stdout.splitlines()",
            "payload={'uv':identity(uv) if uv else None,'python':identity(py),'python_version':subprocess.run([py,'--version'],check=True,capture_output=True,text=True).stdout.strip() or subprocess.run([py,'--version'],check=True,capture_output=True,text=True).stderr.strip(),'apt_packages':sorted(apt)}",
            "print(json.dumps(payload,sort_keys=True))",
        )
    )
    result = _run_task_artifact(
        _docker(
            docker,
            docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--env",
            "UV_OFFLINE=1",
            "--entrypoint",
            "/bin/bash",
            image_id,
            "-c",
            'arena_python="$(command -v python || command -v python3 || uv python find '
            + shlex.quote(selector)
            + ')"; "$arena_python" -c '
            + shlex.quote("exec(" + repr(script) + ")"),
        ),
        task_id=task_id,
        profile=profile_name,
        artifact="resolved-runtime-inventory",
        timeout=300.0,
    )
    try:
        value = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: runtime inventory is malformed"
        ) from exc
    if not isinstance(value, dict) or not isinstance(value.get("python"), dict):
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: runtime inventory is incomplete"
        )
    return MappingProxyType(value)


def _deb_metadata(path: Path) -> Mapping[str, object]:
    fields = {
        name.lower(): _run(
            ("dpkg-deb", "--field", str(path), name), timeout=60.0
        ).stdout.strip()
        for name in ("Package", "Version", "Architecture")
    }
    if not all(fields.values()):
        raise ClosureBuilderError(f"apt package identity is absent: {path.name}")
    return MappingProxyType(
        {
            "filename": path.name,
            "sha256": _sha256_file(path),
            "size": path.stat().st_size,
            **fields,
        }
    )


def _resolve_apt_artifacts(
    *,
    arguments: argparse.Namespace,
    cache_root: Path,
    task_id: str,
    profile_name: str,
    base_image_id: str,
    packages: Sequence[str],
    timeout: float,
) -> list[dict[str, object]]:
    debs = cache_root / "apt" / task_id
    if debs.exists():
        shutil.rmtree(debs)
    debs.mkdir(mode=0o755, parents=True)
    if not packages:
        return []
    script = (
        "set -eu; mkdir -p /debs/partial; apt-get update >/dev/null; "
        "DEBIAN_FRONTEND=noninteractive apt-get -y --download-only "
        "-o Dir::Cache::archives=/debs install "
        + shlex.join(tuple(packages))
        + "; rm -f /debs/lock; rm -rf /debs/partial"
    )
    _run_task_artifact(
        _docker(
            arguments.docker,
            arguments.docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "bridge",
            "--mount",
            f"type=bind,src={debs},dst=/debs",
            "--entrypoint",
            "/bin/bash",
            base_image_id,
            "-lc",
            script,
        ),
        task_id=task_id,
        profile=profile_name,
        artifact="transitive-apt-set",
        timeout=max(timeout, 900.0),
    )
    return [dict(_deb_metadata(path)) for path in sorted(debs.glob("*.deb"))]


def _extract_runtime_artifacts(
    *,
    arguments: argparse.Namespace,
    output: Path,
    cache_root: Path,
    task_id: str,
    profile_name: str,
    profile: Mapping[str, object],
    resolver_image_id: str,
    runtime_inventory: Mapping[str, object],
) -> tuple[list[dict[str, object]], Mapping[str, object]]:
    runtime_root = cache_root / "runtime" / task_id
    if runtime_root.exists():
        shutil.rmtree(runtime_root)
    runtime_root.mkdir(mode=0o755, parents=True)
    python = profile.get("python")
    uv = profile.get("uv")
    if not isinstance(python, Mapping) or not isinstance(uv, Mapping):
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: runtime profile malformed"
        )
    uv_inventory = runtime_inventory.get("uv")
    python_inventory = runtime_inventory.get("python")
    if not isinstance(python_inventory, Mapping):
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: resolved Python identity is absent"
        )
    container = _run(
        _docker(
            arguments.docker,
            arguments.docker_host,
            "create",
            resolver_image_id,
        ),
        timeout=120.0,
    ).stdout.strip()
    if not container:
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: resolver container was not created"
        )
    artifacts: list[dict[str, object]] = []
    try:
        if isinstance(uv_inventory, Mapping):
            uv_path = uv_inventory.get("path")
            if not isinstance(uv_path, str) or not uv_path.startswith("/"):
                raise ClosureBuilderError(
                    f"{task_id}/{profile_name}: resolved uv path is invalid"
                )
            destination = runtime_root / "uv"
            _run_task_artifact(
                _docker(
                    arguments.docker,
                    arguments.docker_host,
                    "cp",
                    f"{container}:{uv_path}",
                    str(destination),
                ),
                task_id=task_id,
                profile=profile_name,
                artifact="uv-binary",
                timeout=300.0,
            )
            destination.chmod(0o755)
            artifacts.append(
                {
                    "kind": "uv-binary",
                    "version": uv.get("version"),
                    "source": uv.get("source", "task-environment"),
                    "path": destination.relative_to(output).as_posix(),
                    "sha256": _sha256_file(destination),
                    "size": destination.stat().st_size,
                }
            )
            uvx_path = str(Path(uv_path).with_name("uvx"))
            uvx_destination = runtime_root / "uvx"
            _run_task_artifact(
                _docker(
                    arguments.docker,
                    arguments.docker_host,
                    "cp",
                    f"{container}:{uvx_path}",
                    str(uvx_destination),
                ),
                task_id=task_id,
                profile=profile_name,
                artifact="uvx-binary",
                timeout=300.0,
            )
            uvx_destination.chmod(0o755)
            artifacts.append(
                {
                    "kind": "uvx-binary",
                    "version": uv.get("version"),
                    "source": uv.get("source", "task-environment"),
                    "path": uvx_destination.relative_to(output).as_posix(),
                    "sha256": _sha256_file(uvx_destination),
                    "size": uvx_destination.stat().st_size,
                }
            )

        python_path = python_inventory.get("path")
        if not isinstance(python_path, str) or not python_path.startswith("/"):
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: resolved Python path is invalid"
            )
        managed_root = "/root/.local/share/uv/python"
        if python_path.startswith(managed_root + "/"):
            extracted = runtime_root / "python-installations"
            extracted.mkdir(mode=0o755)
            _run_task_artifact(
                _docker(
                    arguments.docker,
                    arguments.docker_host,
                    "cp",
                    f"{container}:{managed_root}/.",
                    str(extracted),
                ),
                task_id=task_id,
                profile=profile_name,
                artifact="uv-python-runtime",
                timeout=900.0,
            )
            archive = Path(
                shutil.make_archive(
                    str(runtime_root / "python-installations"),
                    "tar",
                    root_dir=extracted,
                )
            )
            shutil.rmtree(extracted)
            python_runtime: Mapping[str, object] = MappingProxyType(
                {
                    "provider": "locked-uv-installation",
                    "selector": python.get("selector"),
                    "python_version": runtime_inventory.get("python_version"),
                    "executable_relative": python_path[len(managed_root) + 1 :],
                    "path": archive.relative_to(output).as_posix(),
                    "sha256": _sha256_file(archive),
                    "size": archive.stat().st_size,
                }
            )
            artifacts.append(dict(python_runtime))
        else:
            python_runtime = MappingProxyType(
                {
                    "provider": "locked-base-image",
                    "selector": python.get("selector"),
                    "python_version": runtime_inventory.get("python_version"),
                    "path": python_path,
                    "sha256": python_inventory.get("sha256"),
                    "size": python_inventory.get("size"),
                }
            )
    finally:
        _run(
            _docker(
                arguments.docker,
                arguments.docker_host,
                "rm",
                "--force",
                container,
            ),
            timeout=120.0,
            check=False,
        )
    return artifacts, python_runtime


def _content_locked_recipe(
    *,
    arguments: argparse.Namespace,
    output: Path,
    cache_root: Path,
    task_id: str,
    profile_name: str,
    profile: Mapping[str, object],
    family: str,
    image_id: str,
    timeout: float,
) -> Mapping[str, object]:
    """Resolve public artifacts online for a later network-free image build.

    The temporary resolver image is never part of the lock or public build.
    Only independently hashed packages, runtimes, VCS bundles, and assets are
    retained as build inputs.
    """

    input_root = cache_root / "recipe-inputs" / task_id
    if input_root.exists():
        shutil.rmtree(input_root)
    acquisitions = _resolve_recipe_inputs(
        task_id=task_id,
        profile_name=profile_name,
        profile=profile,
        root=input_root,
    )
    context = output / "lock-contexts" / task_id
    if context.exists():
        shutil.rmtree(context)
    (context / "inputs").mkdir(mode=0o755, parents=True)
    for path in sorted(input_root.iterdir()) if input_root.exists() else ():
        if not path.is_file() or path.is_symlink():
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}/recipe-input:{path.name}: invalid file"
            )
        shutil.copyfile(path, context / "inputs" / path.name)
    base_tag = f"rsi-arena/tb21-lock-base-{task_id}:{image_id[7:19]}"
    _run(_docker(arguments.docker, arguments.docker_host, "tag", image_id, base_tag))
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(
        _render_online_resolver_dockerfile(
            base_tag=base_tag,
            task_id=task_id,
            profile_name=profile_name,
            profile=profile,
            family=family,
            acquisitions=acquisitions,
        ),
        "utf-8",
    )
    tag = f"rsi-arena/tb21-resolved-{task_id}:{_sha256_file(dockerfile)[7:19]}"
    _run_task_artifact(
        _docker(
            arguments.docker,
            arguments.docker_host,
            "build",
            "--network=default",
            "--pull=false",
            "--no-cache",
            "--tag",
            tag,
            str(context),
        ),
        task_id=task_id,
        profile=profile_name,
        artifact="resolved-verifier-input-layer",
        timeout=max(timeout, 3600.0),
    )
    identity = _inspect_image(arguments.docker, arguments.docker_host, tag)
    resolved_id = str(identity["Id"])
    apt = profile.get("apt")
    pypi = profile.get("pypi")
    python = profile.get("python")
    uv = profile.get("uv")
    if not all(isinstance(value, Mapping) for value in (apt, pypi, python, uv)):
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: resolver profile malformed"
        )
    assert isinstance(apt, Mapping)
    assert isinstance(pypi, Mapping)
    assert isinstance(python, Mapping)
    assert isinstance(uv, Mapping)
    requirements = tuple(str(value) for value in pypi.get("requirements", ()))
    indexes = tuple(str(value) for value in pypi.get("indexes", ()))
    packages = tuple(str(value) for value in apt.get("packages", ()))
    deb_artifacts = _resolve_apt_artifacts(
        arguments=arguments,
        cache_root=cache_root,
        task_id=task_id,
        profile_name=profile_name,
        base_image_id=image_id,
        packages=packages,
        timeout=timeout,
    )
    wheels = cache_root / "wheels" / task_id
    if wheels.exists():
        shutil.rmtree(wheels)
    wheels.mkdir(mode=0o755, parents=True)
    if uv.get("mode") == "not-used":
        resolver = ["python", "-m", "pip"]
    else:
        resolver = [
            "uvx",
            "-p",
            str(python.get("selector")),
            "--from",
            "pip==25.2",
            "pip",
        ]
    locked_sdists = {
        str(record["requirement"]): (
            "/opt/rsi-arena/tb21/sdists/" + str(record["path"])
        )
        for record in acquisitions
        if record.get("kind") == "pypi-sdist"
    }
    source_requirements = []
    if task_id == "sam-cell-seg":
        vcs = next(item for item in acquisitions if item.get("kind") == "git-bundle")
        source_requirements.append(f"git+{vcs['url']}@{vcs['resolved_commit']}")
    if locked_sdists or source_requirements:
        resolver.extend(
            (
                "--disable-pip-version-check",
                "wheel",
                "--wheel-dir",
                "/wheels",
            )
        )
    else:
        resolver.extend(
            (
                "--disable-pip-version-check",
                "download",
                "--only-binary=:all:",
                "--dest",
                "/wheels",
            )
        )
    nondefault_indexes = [value for value in indexes if value != "pypi-default"]
    if nondefault_indexes:
        if nondefault_indexes != [PYTORCH_CPU_INDEX]:
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: unsupported wheel index set"
            )
        resolver.extend(
            (
                "--index-url",
                PYTORCH_CPU_INDEX,
                "--extra-index-url",
                "https://pypi.org/simple",
            )
        )
    resolver.extend(locked_sdists.get(value, value) for value in requirements)
    resolver.extend(source_requirements)
    _run_task_artifact(
        _docker(
            arguments.docker,
            arguments.docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "bridge",
            "--env",
            "UV_OFFLINE=0",
            "--mount",
            f"type=bind,src={wheels},dst=/wheels",
            "--entrypoint",
            "/bin/bash",
            resolved_id,
            "-c",
            shlex.join(resolver),
        ),
        task_id=task_id,
        profile=profile_name,
        artifact="resolved-wheel-set",
        timeout=max(timeout, 1800.0),
    )
    wheel_artifacts = [
        dict(_wheel_metadata(path)) for path in sorted(wheels.glob("*.whl"))
    ]
    if not wheel_artifacts:
        raise ClosureBuilderError(f"{task_id}/{profile_name}: no wheels resolved")
    source_built_requirements: list[str] = []
    if task_id == "sam-cell-seg":
        matches = [
            item
            for item in wheel_artifacts
            if re.sub(r"[-_.]+", "-", str(item["name"]).lower()) == "mobile-sam"
        ]
        if len(matches) != 1:
            raise ClosureBuilderError(
                "sam-cell-seg: source-built MobileSAM wheel is not unique"
            )
        source_built_requirements.append(
            f"{matches[0]['name']}=={matches[0]['version']}"
        )
    runtime_inventory = _resolved_runtime_inventory(
        docker=arguments.docker,
        docker_host=arguments.docker_host,
        image_id=resolved_id,
        selector=str(python.get("selector")),
        task_id=task_id,
        profile_name=profile_name,
    )
    runtime_artifacts, python_runtime = _extract_runtime_artifacts(
        arguments=arguments,
        output=output,
        cache_root=cache_root,
        task_id=task_id,
        profile_name=profile_name,
        profile=profile,
        resolver_image_id=resolved_id,
        runtime_inventory=runtime_inventory,
    )
    locked_acquisitions: list[dict[str, object]] = []
    for raw in acquisitions:
        record = dict(raw)
        filename = record.get("path")
        if isinstance(filename, str):
            record["path"] = (input_root / filename).relative_to(output).as_posix()
        locked_acquisitions.append(record)
    artifact_inputs = {
        str(item.get("path", item.get("url", index))): item.get("sha256")
        for index, item in enumerate(locked_acquisitions)
    }
    artifact_inputs.update(
        {
            (wheels / str(item["filename"])).relative_to(output).as_posix(): item[
                "sha256"
            ]
            for item in wheel_artifacts
        }
    )
    artifact_inputs.update(
        {
            (cache_root / "apt" / task_id / str(item["filename"]))
            .relative_to(output)
            .as_posix(): item["sha256"]
            for item in deb_artifacts
        }
    )
    artifact_inputs.update(
        {str(item["path"]): item["sha256"] for item in runtime_artifacts}
    )
    return MappingProxyType(
        {
            "family": family,
            "build_strategy": "content-locked-artifacts",
            "acquisitions": locked_acquisitions,
            "artifacts": wheel_artifacts,
            "apt_artifacts": deb_artifacts,
            "apt_packages": list(packages),
            "python_selector": python.get("selector"),
            "python_runtime": dict(python_runtime),
            "uv_mode": uv.get("mode"),
            "uv_version": uv.get("version"),
            "pypi_indexes": list(indexes),
            "pypi_index_strategy": pypi.get("index_strategy"),
            "runtime_artifacts": runtime_artifacts,
            "runtime_inventory": dict(runtime_inventory),
            "source_built_requirements": source_built_requirements,
            "artifact_set_digest": _canonical_digest(artifact_inputs),
        }
    )


def _lock_command(arguments: argparse.Namespace) -> Path:
    repository = Path(arguments.task_repository).expanduser().resolve(strict=True)
    output = _require_output_root(Path(arguments.output_dir))
    snapshot = load_tb21_task_snapshot(repository)
    if _git(repository, "rev-parse", "HEAD") != snapshot.commit:
        raise ClosureBuilderError("task repository HEAD is not the pinned TB2.1 commit")
    if _git(repository, "status", "--porcelain=v1"):
        raise ClosureBuilderError("task repository must be clean before locking")
    census = _load_json(VERIFIER_DEPENDENCY_CLOSURE_PATH)
    task_ids = _select_tasks(census, arguments.task, arguments.family)
    final_path = output / "closure-lock.json"
    if final_path.exists():
        lock = _load_and_validate_lock(final_path)
        if tuple(sorted(lock["tasks"])) != task_ids:
            raise ClosureBuilderError("existing lock selected a different task set")
        _validate_locked_cache(final_path, lock)
        return final_path
    _write_state(output, phase="locking", task_ids=task_ids)
    cache_root = output / "artifact-cache"
    sources_root = output / "task-sources"
    cache_root.mkdir(mode=0o755, exist_ok=True)
    sources_root.mkdir(mode=0o755, exist_ok=True)
    locked_tasks: dict[str, object] = {}
    for task_id in task_ids:
        profile_name, census_task, profile = _profile_for_task(census, task_id)
        family = _profile_family(task_id, profile_name, profile)
        requirements = _wheel_only_requirements(task_id, profile_name, profile)
        manifest = _task_manifest(repository, task_id)
        environment = manifest.get("environment")
        if not isinstance(environment, Mapping):
            raise ClosureBuilderError(f"{task_id}: environment config is unavailable")
        image_ref = environment.get("docker_image")
        if image_ref != census_task.get("environment_image") or not isinstance(
            image_ref, str
        ):
            raise ClosureBuilderError(f"{task_id}: environment image drifted")
        environment_oid = _git(
            repository, "rev-parse", f"HEAD:tasks/{task_id}/environment"
        )
        tests_oid = _git(repository, "rev-parse", f"HEAD:tasks/{task_id}/tests")
        if environment_oid != census_task.get(
            "environment_tree_git_oid"
        ) or tests_oid != census_task.get("tests_tree_git_oid"):
            raise ClosureBuilderError(f"{task_id}: task source tree drifted")
        fragment_path = output / "task-locks" / f"{task_id}.json"
        if fragment_path.exists():
            fragment = _load_json(fragment_path)
            raw_fragment = fragment.get("task")
            if (
                fragment.get("schema") != LOCK_SCHEMA
                or fragment.get("task_id") != task_id
                or not isinstance(raw_fragment, dict)
                or raw_fragment.get("profile") != profile_name
                or raw_fragment.get("family") != family
                or raw_fragment.get("environment_tree_git_oid") != environment_oid
                or raw_fragment.get("tests_tree_git_oid") != tests_oid
            ):
                raise ClosureBuilderError(f"{task_id}: partial lock identity changed")
            _validate_locked_cache(
                output / ".partial-lock-validation.json",
                {"tasks": {task_id: raw_fragment}},
            )
            locked_tasks[task_id] = raw_fragment
            _write_state(output, phase="locking", task_ids=tuple(locked_tasks))
            continue
        copied = sources_root / task_id
        if copied.exists():
            shutil.rmtree(copied)
        shutil.copytree(repository / "tasks" / task_id, copied, symlinks=True)
        _run_task_artifact(
            _docker(arguments.docker, arguments.docker_host, "pull", image_ref),
            task_id=task_id,
            profile=profile_name,
            artifact=f"base-image:{image_ref}",
            timeout=float(environment.get("build_timeout_sec", 1800.0)),
        )
        image = _inspect_image(arguments.docker, arguments.docker_host, image_ref)
        image_id = str(image["Id"])
        platform = f"{image.get('Os')}/{image.get('Architecture')}"
        if platform != SUPPORTED_PLATFORM:
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: unsupported base platform {platform}"
            )
        base_dir = cache_root / "base-images"
        base_dir.mkdir(mode=0o755, exist_ok=True)
        archive = base_dir / f"{task_id}.tar"
        _run_task_artifact(
            _docker(
                arguments.docker,
                arguments.docker_host,
                "save",
                "--output",
                str(archive),
                image_id,
            ),
            task_id=task_id,
            profile=profile_name,
            artifact="base-image-archive",
            timeout=1800.0,
        )
        recipe: Mapping[str, object]
        artifacts: list[dict[str, object]] = []
        if requirements is not None:
            wheels = cache_root / "wheels" / task_id
            if wheels.exists() or wheels.is_symlink():
                if wheels.is_symlink() or not wheels.is_dir():
                    raise ClosureBuilderError(f"{task_id}: wheel cache path is invalid")
                shutil.rmtree(wheels)
            wheels.mkdir(mode=0o755, parents=True, exist_ok=True)
            download = list(
                _docker(
                    arguments.docker,
                    arguments.docker_host,
                    "run",
                    "--rm",
                    "--pull=never",
                    "--network",
                    "bridge",
                    "--user",
                    f"{os.getuid()}:{os.getgid()}",
                    "--env",
                    "HOME=/tmp",
                    "--mount",
                    f"type=bind,src={wheels},dst=/wheels",
                    "--entrypoint",
                    "python",
                    image_id,
                    "-m",
                    "pip",
                    "download",
                    "--disable-pip-version-check",
                    "--only-binary=:all:",
                    "--dest",
                    "/wheels",
                )
            )
            download.extend(requirements)
            _run_task_artifact(
                tuple(download),
                task_id=task_id,
                profile=profile_name,
                artifact="pypi-wheel-set",
                timeout=900.0,
            )
            artifacts = [
                dict(_wheel_metadata(path)) for path in sorted(wheels.glob("*.whl"))
            ]
            if not artifacts:
                raise ClosureBuilderError(f"{task_id}: PyPI resolver fetched no wheels")
            recipe = MappingProxyType(
                {
                    "family": family,
                    "build_strategy": "wheel-files",
                    "acquisitions": [],
                    "artifact_set_digest": _canonical_digest(
                        {item["filename"]: item["sha256"] for item in artifacts}
                    ),
                }
            )
        else:
            recipe = _content_locked_recipe(
                arguments=arguments,
                output=output,
                cache_root=cache_root,
                task_id=task_id,
                profile_name=profile_name,
                profile=profile,
                family=family,
                image_id=image_id,
                timeout=float(environment.get("build_timeout_sec", 1800.0)),
            )
            resolved_artifacts = recipe.get("artifacts")
            if not isinstance(resolved_artifacts, list):
                raise ClosureBuilderError(
                    f"{task_id}/{profile_name}: wheel content lock is absent"
                )
            artifacts = [dict(item) for item in resolved_artifacts]
        test_path = repository / "tasks" / task_id / "tests" / "test.sh"
        locked_task = {
            **dict(recipe),
            "profile": profile_name,
            "categories": list(census_task.get("categories", [])),
            "task_material_digest": task_tree_digest(repository / "tasks" / task_id),
            "environment_tree_git_oid": environment_oid,
            "tests_tree_git_oid": tests_oid,
            "test_digest": _sha256_file(test_path),
            "configured_base_image": image_ref,
            "resolved_base_image_id": image_id,
            "resolved_repo_digests": sorted(image.get("RepoDigests") or []),
            "platform": platform,
            "base_image_archive": {
                "path": archive.relative_to(output).as_posix(),
                "sha256": _sha256_file(archive),
                "size": archive.stat().st_size,
            },
            "verifier_requirements": (
                list(requirements)
                if requirements is not None
                else list(profile["pypi"]["requirements"])
                + list(recipe.get("source_built_requirements", ()))
            ),
            "artifacts": artifacts,
            "dynamic_candidate_uv": task_id == "reshard-c4-data",
        }
        locked_tasks[task_id] = locked_task
        _atomic_json(
            fragment_path,
            {
                "schema": LOCK_SCHEMA,
                "task_id": task_id,
                "task": locked_task,
            },
        )
        _write_state(output, phase="locking", task_ids=tuple(locked_tasks))
    lock: dict[str, object] = {
        "schema": LOCK_SCHEMA,
        "task_source": {
            "repository": "harbor-framework/terminal-bench-2-1",
            "commit": snapshot.commit,
            "repository_tree": snapshot.repository_tree,
            "tasks_tree": snapshot.tasks_tree,
        },
        "dependency_census_digest": _sha256_file(VERIFIER_DEPENDENCY_CLOSURE_PATH),
        "platform": SUPPORTED_PLATFORM,
        "tasks": locked_tasks,
    }
    lock["lock_digest"] = _canonical_digest(lock)
    _atomic_json(final_path, lock)
    _write_state(output, phase="locked", task_ids=task_ids)
    return final_path


def _load_and_validate_lock(path: Path) -> dict[str, Any]:
    lock = _load_json(path)
    claimed = lock.get("lock_digest")
    digest_input = dict(lock)
    digest_input.pop("lock_digest", None)
    task_source = lock.get("task_source")
    tasks = lock.get("tasks")
    if (
        set(lock)
        != {
            "schema",
            "task_source",
            "dependency_census_digest",
            "platform",
            "tasks",
            "lock_digest",
        }
        or lock.get("schema") != LOCK_SCHEMA
        or not isinstance(claimed, str)
        or _canonical_digest(digest_input) != claimed
        or lock.get("dependency_census_digest")
        != _sha256_file(VERIFIER_DEPENDENCY_CLOSURE_PATH)
        or lock.get("platform") != SUPPORTED_PLATFORM
        or not isinstance(task_source, Mapping)
        or set(task_source) != {"repository", "commit", "repository_tree", "tasks_tree"}
        or task_source.get("repository") != "harbor-framework/terminal-bench-2-1"
        or any(
            not isinstance(task_source.get(name), str)
            or re.fullmatch(r"[0-9a-f]{40}", str(task_source[name])) is None
            for name in ("commit", "repository_tree", "tasks_tree")
        )
        or not isinstance(tasks, dict)
        or not tasks
        or any(
            not isinstance(task_id, str)
            or _TASK_ID_RE.fullmatch(task_id) is None
            or not isinstance(raw, Mapping)
            or not isinstance(raw.get("task_material_digest"), str)
            or re.fullmatch(r"[0-9a-f]{64}", str(raw.get("task_material_digest")))
            is None
            or any(
                not isinstance(raw.get(name), str)
                or _SHA256_RE.fullmatch(str(raw[name])) is None
                for name in (
                    "test_digest",
                    "resolved_base_image_id",
                    "artifact_set_digest",
                )
            )
            or not isinstance(raw.get("configured_base_image"), str)
            or not raw.get("configured_base_image")
            or not isinstance(raw.get("dynamic_candidate_uv"), bool)
            for task_id, raw in tasks.items()
        )
    ):
        raise ClosureBuilderError("closure lock is invalid")
    return lock


def _selected_locked_tasks(
    lock: Mapping[str, object], requested: Sequence[str]
) -> tuple[str, ...]:
    tasks = lock["tasks"]
    assert isinstance(tasks, Mapping)
    selected = tuple(sorted(set(requested))) if requested else tuple(sorted(tasks))
    missing = sorted(set(selected) - set(tasks))
    if missing:
        raise ClosureBuilderError(f"lock has no task: {missing[0]}")
    return selected


def _validate_cache_file(root: Path, record: Mapping[str, object], label: str) -> Path:
    relative = record.get("path")
    expected = record.get("sha256")
    if not isinstance(relative, str) or not isinstance(expected, str):
        raise ClosureBuilderError(f"{label}: cache record is malformed")
    try:
        path = (root / relative).resolve(strict=True)
    except OSError as exc:
        raise ClosureBuilderError(f"{label}: cache artifact is missing") from exc
    try:
        path.relative_to(root.resolve(strict=True))
    except ValueError as exc:
        raise ClosureBuilderError(f"{label}: cache path escaped the lock") from exc
    if _sha256_file(path) != expected:
        raise ClosureBuilderError(f"{label}: cache artifact digest changed")
    return path


def _validate_locked_cache(path: Path, lock: Mapping[str, object]) -> None:
    root = path.parent
    tasks = lock.get("tasks")
    assert isinstance(tasks, Mapping)
    for task_id, raw in tasks.items():
        if not isinstance(raw, Mapping):
            raise ClosureBuilderError(f"{task_id}: lock entry is malformed")
        archive = raw.get("base_image_archive")
        artifacts = raw.get("artifacts")
        if not isinstance(archive, Mapping) or not isinstance(artifacts, list):
            raise ClosureBuilderError(f"{task_id}: cached artifacts are absent")
        _validate_cache_file(root, archive, f"{task_id}/base-image")
        task_source = root / "task-sources" / str(task_id)
        try:
            source_digest = task_tree_digest(task_source)
        except (OSError, ValueError) as exc:
            raise ClosureBuilderError(
                f"{task_id}: cached task source is invalid"
            ) from exc
        if source_digest != raw.get("task_material_digest"):
            raise ClosureBuilderError(f"{task_id}: cached task source changed")
        strategy = raw.get("build_strategy")
        if strategy == "content-locked-artifacts":
            acquisitions = raw.get("acquisitions")
            runtime_artifacts = raw.get("runtime_artifacts")
            if not isinstance(acquisitions, list) or not isinstance(
                runtime_artifacts, list
            ):
                raise ClosureBuilderError(f"{task_id}: content lock is absent")
            for index, acquisition in enumerate(acquisitions):
                if not isinstance(acquisition, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: acquisition record is malformed"
                    )
                _validate_cache_file(
                    root, acquisition, f"{task_id}/acquisition-{index}"
                )
            for index, artifact in enumerate(runtime_artifacts):
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: runtime artifact record is malformed"
                    )
                _validate_cache_file(root, artifact, f"{task_id}/runtime-{index}")
            try:
                resolved_wheels = (
                    root / "artifact-cache" / "wheels" / str(task_id)
                ).resolve(strict=True)
            except OSError as exc:
                raise ClosureBuilderError(f"{task_id}: wheel cache is missing") from exc
            for artifact in artifacts:
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(f"{task_id}: wheel lock is malformed")
                filename = artifact.get("filename")
                digest = artifact.get("sha256")
                if not isinstance(filename, str) or not isinstance(digest, str):
                    raise ClosureBuilderError(f"{task_id}: wheel identity is malformed")
                wheel = (resolved_wheels / filename).resolve(strict=True)
                if wheel.parent != resolved_wheels or _sha256_file(wheel) != digest:
                    raise ClosureBuilderError(f"{task_id}: wheel changed: {filename}")
            apt_artifacts = raw.get("apt_artifacts")
            if not isinstance(apt_artifacts, list):
                raise ClosureBuilderError(f"{task_id}: apt content lock is absent")
            apt_root = root / "artifact-cache" / "apt" / str(task_id)
            for artifact in apt_artifacts:
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: apt content record is malformed"
                    )
                filename = artifact.get("filename")
                digest = artifact.get("sha256")
                if not isinstance(filename, str) or not isinstance(digest, str):
                    raise ClosureBuilderError(
                        f"{task_id}: apt content identity is malformed"
                    )
                package = (apt_root / filename).resolve(strict=True)
                if (
                    package.parent != apt_root.resolve()
                    or _sha256_file(package) != digest
                ):
                    raise ClosureBuilderError(
                        f"{task_id}: apt content changed: {filename}"
                    )
            continue
        if strategy != "wheel-files":
            raise ClosureBuilderError(f"{task_id}: unknown build strategy")
        try:
            wheel_root = (root / "artifact-cache" / "wheels" / str(task_id)).resolve(
                strict=True
            )
        except OSError as exc:
            raise ClosureBuilderError(f"{task_id}: wheel cache is missing") from exc
        for artifact in artifacts:
            if not isinstance(artifact, Mapping):
                raise ClosureBuilderError(f"{task_id}: wheel lock is malformed")
            filename = artifact.get("filename")
            digest = artifact.get("sha256")
            if not isinstance(filename, str) or not isinstance(digest, str):
                raise ClosureBuilderError(f"{task_id}: wheel identity is malformed")
            try:
                wheel = (wheel_root / filename).resolve(strict=True)
            except OSError as exc:
                raise ClosureBuilderError(
                    f"{task_id}: wheel artifact is missing: {filename}"
                ) from exc
            if wheel.parent != wheel_root or _sha256_file(wheel) != digest:
                raise ClosureBuilderError(
                    f"{task_id}: wheel artifact changed: {filename}"
                )


def _decode_builder_authority(
    path: Path, *, require_verified: bool
) -> OfflineClosureAuthority:
    """Translate the one shared decoder error into the builder CLI domain."""

    try:
        return decode_offline_closure_authority(path, require_verified=require_verified)
    except TB21OfflineVerifierError as exc:
        raise ClosureBuilderError("offline verifier authority is invalid") from exc


def _load_and_validate_authority(path: Path) -> dict[str, Any]:
    """Return the canonical payload through the sole authority decoder."""

    return dict(_decode_builder_authority(path, require_verified=False).payload)


def _builder_authority_payload(
    *,
    task_source: Mapping[str, object],
    lock_digest: object,
    platform: object,
    tasks: Mapping[str, Mapping[str, object]],
) -> dict[str, Any]:
    """Emit the canonical built-stage authority used by every later stage."""

    task_ids = tuple(sorted(tasks))
    built = {task_id: dict(tasks[task_id]) for task_id in task_ids}
    authority: dict[str, Any] = {
        "schema": AUTHORITY_SCHEMA,
        "generated_at": _utc_now(),
        "task_source": dict(task_source),
        "lock_digest": lock_digest,
        "platform": platform,
        "closure_digest": _canonical_digest(
            {
                task_id: {
                    "base": value["closure_base_image_id"],
                    "closure": value["closure_image_id"],
                    "build_input": value["build_input_digest"],
                }
                for task_id, value in built.items()
            }
        ),
        "tasks": built,
        "verification": {
            "status": "built",
            "network_mode": "none",
            "task_ids": [],
        },
    }
    authority["authority_digest"] = _canonical_digest(authority)
    return authority


def load_verified_exact_image_closure(path: Path) -> Mapping[str, object]:
    """Return exact image IDs from one fully verified canonical authority."""

    authority = _decode_builder_authority(
        Path(path).expanduser(), require_verified=True
    )
    images = MappingProxyType(
        {
            task_id: MappingProxyType(
                {
                    "base": binding.closure_base_image_id,
                    "closure": binding.closure_image_id,
                }
            )
            for task_id, binding in authority.tasks.items()
        }
    )
    return MappingProxyType(
        {
            "path": authority.path,
            "schema": AUTHORITY_SCHEMA,
            "file_digest": authority.digest,
            "content_digest": authority.content_digest,
            "lock_digest": authority.lock_digest,
            "closure_digest": authority.closure_digest,
            "task_source": MappingProxyType(
                {
                    "repository": "harbor-framework/terminal-bench-2-1",
                    "commit": authority.source_commit,
                    "repository_tree": authority.source_tree,
                    "tasks_tree": authority.tasks_tree,
                }
            ),
            "task_ids": tuple(authority.tasks),
            "images": images,
        }
    )


def _render_closure_dockerfile(
    *, base_tag: str, requirements: Sequence[str], task_id: str, lock_digest: str
) -> str:
    requirement_text = " ".join(requirements)
    return (
        f"FROM {base_tag}\n"
        "USER root\n"
        "COPY wheels/ /opt/rsi-arena/tb21/wheels/\n"
        "RUN PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
        "python -m pip install --no-index "
        "--find-links=/opt/rsi-arena/tb21/wheels "
        f"{requirement_text} && python -m pip check\n"
        f'LABEL org.rsi_arena.tb21.task="{task_id}" \\\n'
        f'    org.rsi_arena.tb21.lock="{lock_digest}" \\\n'
        '    org.rsi_arena.tb21.role="verifier-only"\n'
    )


def _render_artifact_closure_dockerfile(
    *,
    base_tag: str,
    task_id: str,
    profile_name: str,
    lock_digest: str,
    requirements: Sequence[str],
    apt_artifacts: Sequence[Mapping[str, object]],
    runtime_artifacts: Sequence[Mapping[str, object]],
    python_runtime: Mapping[str, object],
    python_selector: str,
    uv_mode: str,
    pypi_indexes: Sequence[str],
    pypi_index_strategy: str,
    acquisitions: Sequence[Mapping[str, object]],
) -> str:
    """Render a closure image that consumes only locked local artifacts."""

    lines = [
        f"FROM {base_tag}",
        "USER root",
        'SHELL ["/bin/bash", "-o", "pipefail", "-c"]',
    ]
    if apt_artifacts:
        lines.extend(
            (
                "COPY debs/ /opt/rsi-arena/tb21/debs/",
                "RUN DEBIAN_FRONTEND=noninteractive apt-get -y "
                "install /opt/rsi-arena/tb21/debs/*.deb",
            )
        )
    lines.append("COPY wheels/ /opt/rsi-arena/tb21/wheels/")

    if uv_mode != "not-used":
        if not all(
            any(item.get("kind") == kind for item in runtime_artifacts)
            for kind in ("uv-binary", "uvx-binary")
        ):
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: uv artifact is absent"
            )
        lines.extend(
            (
                "COPY runtime/uv /root/.local/bin/uv",
                "COPY runtime/uvx /root/.local/bin/uvx",
                "RUN chmod 0755 /root/.local/bin/uv "
                "/root/.local/bin/uvx "
                "&& printf '%s\\n' 'export PATH=\"$HOME/.local/bin:$PATH\"' "
                "> /root/.local/bin/env",
                'ENV PATH="/root/.local/bin:${PATH}"',
            )
        )
    provider = python_runtime.get("provider")
    if provider == "locked-uv-installation":
        if not any(
            item.get("provider") == "locked-uv-installation"
            for item in runtime_artifacts
        ):
            raise ClosureBuilderError(
                f"{task_id}/{profile_name}: Python runtime artifact is absent"
            )
        lines.extend(
            (
                "COPY runtime/python-installations.tar "
                "/opt/rsi-arena/tb21/python-installations.tar",
                "RUN mkdir -p /opt/rsi-arena/tb21/python "
                "&& tar -xf /opt/rsi-arena/tb21/python-installations.tar "
                "-C /opt/rsi-arena/tb21/python",
                'ENV UV_PYTHON_INSTALL_DIR="/opt/rsi-arena/tb21/python"',
            )
        )
    elif provider != "locked-base-image":
        raise ClosureBuilderError(
            f"{task_id}/{profile_name}: Python runtime provider is invalid"
        )

    bundle_records = [item for item in acquisitions if item.get("kind") == "git-bundle"]
    if bundle_records:
        lines.append("RUN mkdir -p /opt/rsi-arena/tb21/vcs")
        for index, record in enumerate(bundle_records):
            filename = Path(str(record["path"])).name
            target = f"/opt/rsi-arena/tb21/vcs/source-{index}.bundle"
            mirror = f"/opt/rsi-arena/tb21/vcs/source-{index}.git"
            lines.append(f"COPY inputs/{filename} {target}")
            lines.append(
                "RUN echo "
                + shlex.quote(str(record["sha256"])[7:] + "  " + target)
                + " | sha256sum -c - && git clone --mirror "
                + shlex.quote(target)
                + " "
                + shlex.quote(mirror)
                + " && git config --system protocol.file.allow always"
                + " && git config --system url."
                + shlex.quote("file://" + mirror)
                + ".insteadOf "
                + shlex.quote(str(record["url"]))
            )

    if task_id == "filter-js-from-html":
        archive = next(
            item for item in acquisitions if item.get("kind") == "filter-js-archive"
        )
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{Path(str(archive['path'])).name} /opt/rsi-arena/tb21/assets/filter-js.zip",
                f"COPY inputs/{Path(str(hook['path'])).name} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
            )
        )
    elif task_id == "sam-cell-seg":
        weight = next(item for item in acquisitions if item.get("kind") == "sam-weight")
        wrapper = next(
            item for item in acquisitions if item.get("kind") == "curl-wrapper"
        )
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/bin",
                f"COPY inputs/{Path(str(weight['path'])).name} /opt/rsi-arena/tb21/assets/mobile_sam.pt",
                f"COPY inputs/{Path(str(wrapper['path'])).name} /opt/rsi-arena/tb21/bin/curl",
                "RUN chmod 0755 /opt/rsi-arena/tb21/bin/curl",
                'ENV PATH="/opt/rsi-arena/tb21/bin:/root/.local/bin:${PATH}"',
            )
        )
    elif task_id == "pytorch-model-cli":
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets/mnist/raw /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{Path(str(hook['path'])).name} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
            )
        )
        for record in acquisitions:
            if record.get("kind") != "mnist-resource":
                continue
            filename = Path(str(record["path"])).name
            destination = "/opt/rsi-arena/tb21/assets/mnist/raw/" + filename
            lines.append(f"COPY inputs/{filename} {destination}")
            script = (
                "import gzip,hashlib,pathlib;"
                f"p=pathlib.Path({destination!r});d=gzip.decompress(p.read_bytes());"
                f"assert hashlib.sha256(d).hexdigest()=={str(record['extracted_sha256'])[7:]!r};"
                "p.with_suffix('').write_bytes(d)"
            )
            lines.append("RUN python -c " + shlex.quote(script))
    elif task_id == "reshard-c4-data":
        shard = next(item for item in acquisitions if item.get("kind") == "c4-shard")
        hook = next(item for item in acquisitions if item.get("kind") == "python-hook")
        shard_name = Path(str(shard["path"])).name
        lines.extend(
            (
                "RUN mkdir -p /opt/rsi-arena/tb21/assets /opt/rsi-arena/tb21/hooks",
                f"COPY inputs/{shard_name} /opt/rsi-arena/tb21/assets/{shard_name}",
                f"COPY inputs/{Path(str(hook['path'])).name} /opt/rsi-arena/tb21/hooks/sitecustomize.py",
                'ENV PYTHONPATH="/opt/rsi-arena/tb21/hooks"',
                "ENV HF_HUB_OFFLINE=1 HF_DATASETS_OFFLINE=1",
            )
        )

    requirement_text = shlex.join(tuple(requirements))
    if uv_mode == "not-used":
        lines.append(
            "RUN PIP_NO_INDEX=1 PIP_DISABLE_PIP_VERSION_CHECK=1 "
            "python -m pip install --no-index "
            "--find-links=/opt/rsi-arena/tb21/wheels "
            + requirement_text
            + " && python -m pip check"
        )
    else:
        lines.append(
            'ENV UV_OFFLINE=1 UV_NO_INDEX=1 UV_FIND_LINKS="/opt/rsi-arena/tb21/wheels"'
        )
        warm = ["uvx"]
        for index in pypi_indexes:
            if index != "pypi-default":
                warm.extend(("--index", index))
        if pypi_index_strategy == "unsafe-best-match":
            warm.extend(("--index-strategy", "unsafe-best-match"))
        warm.extend(("-p", python_selector))
        for requirement in requirements:
            warm.extend(("-w", requirement))
        warm.extend(("pytest", "--version"))
        lines.append("RUN " + shlex.join(warm))
    lines.extend(
        (
            f'LABEL org.rsi_arena.tb21.task="{task_id}"',
            f'LABEL org.rsi_arena.tb21.profile="{profile_name}"',
            f'LABEL org.rsi_arena.tb21.lock="{lock_digest}"',
            'LABEL org.rsi_arena.tb21.role="verifier-only"',
            "",
        )
    )
    return "\n".join(lines)


def _build_command(arguments: argparse.Namespace) -> Path:
    lock_path = Path(arguments.lock).expanduser().resolve(strict=True)
    lock_root = lock_path.parent
    lock = _load_and_validate_lock(lock_path)
    _validate_locked_cache(lock_path, lock)
    task_ids = _selected_locked_tasks(lock, arguments.task)
    output = _require_output_root(Path(arguments.output_dir))
    authority_path = output / "offline-verifier-authority.json"
    if authority_path.exists():
        authority = _decode_builder_authority(authority_path, require_verified=False)
        if (
            authority.lock_digest != lock.get("lock_digest")
            or tuple(authority.tasks) != task_ids
            or dict(authority.payload.get("task_source", {})) != lock.get("task_source")
        ):
            raise ClosureBuilderError(
                "existing build used a different lock or task set"
            )
        try:
            for binding in authority.tasks.values():
                _inspect_image(
                    arguments.docker,
                    arguments.docker_host,
                    binding.closure_base_image_id,
                )
                _inspect_image(
                    arguments.docker,
                    arguments.docker_host,
                    binding.closure_image_id,
                )
        except ClosureBuilderError:
            # The authority is a reproducible cache output, while the lock and
            # retained artifacts are the durable build inputs. If Docker cache
            # eviction removed an image, invalidate the derived authority and
            # resume task-at-a-time reconstruction from the content lock.
            authority_path.unlink()
        else:
            return authority_path
    _write_state(output, phase="building", task_ids=task_ids)
    built: dict[str, object] = {}
    for task_id in task_ids:
        raw = lock["tasks"][task_id]
        if not isinstance(raw, Mapping):
            raise ClosureBuilderError(f"{task_id}: lock entry is malformed")
        build_fragment_path = output / "task-builds" / f"{task_id}.json"
        if build_fragment_path.exists():
            fragment = _load_json(build_fragment_path)
            record = fragment.get("task")
            if (
                fragment.get("schema") != AUTHORITY_SCHEMA
                or fragment.get("task_id") != task_id
                or fragment.get("lock_digest") != lock.get("lock_digest")
                or not isinstance(record, dict)
                or record.get("task_material_digest") != raw.get("task_material_digest")
            ):
                raise ClosureBuilderError(f"{task_id}: partial build identity changed")
            try:
                _inspect_image(
                    arguments.docker,
                    arguments.docker_host,
                    str(record.get("closure_base_image_id")),
                )
                _inspect_image(
                    arguments.docker,
                    arguments.docker_host,
                    str(record.get("closure_image_id")),
                )
                if task_tree_digest(output / "inputs" / task_id) != raw.get(
                    "task_material_digest"
                ):
                    raise ClosureBuilderError(
                        f"{task_id}: partial build task input changed"
                    )
            except ClosureBuilderError:
                pass
            else:
                built[task_id] = record
                _write_state(output, phase="building", task_ids=tuple(built))
                continue
        archive_record = raw.get("base_image_archive")
        if not isinstance(archive_record, Mapping):
            raise ClosureBuilderError(f"{task_id}: base archive is absent")
        archive = _validate_cache_file(
            lock_root, archive_record, f"{task_id}/base-image"
        )
        _run_task_artifact(
            _docker(
                arguments.docker,
                arguments.docker_host,
                "load",
                "--input",
                str(archive),
            ),
            task_id=task_id,
            profile=str(raw.get("profile")),
            artifact="base-image-archive",
            timeout=1800.0,
        )
        base_id = str(raw.get("resolved_base_image_id"))
        observed = _inspect_image(arguments.docker, arguments.docker_host, base_id)
        if observed.get("Id") != base_id:
            raise ClosureBuilderError(f"{task_id}: loaded base image ID changed")
        base_tag = f"rsi-arena/tb21-source-base-{task_id}:{base_id[7:19]}"
        _run(_docker(arguments.docker, arguments.docker_host, "tag", base_id, base_tag))
        context = output / "build-contexts" / task_id
        if context.exists():
            shutil.rmtree(context)
        context.mkdir(mode=0o755, parents=True)
        requirements = raw.get("verifier_requirements")
        if not isinstance(requirements, list) or not all(
            isinstance(item, str) for item in requirements
        ):
            raise ClosureBuilderError(f"{task_id}: verifier requirements are invalid")
        dockerfile = context / "Dockerfile"
        strategy = raw.get("build_strategy")
        if strategy == "wheel-files":
            wheel_source = lock_root / "artifact-cache" / "wheels" / task_id
            wheel_target = context / "wheels"
            wheel_target.mkdir(mode=0o755)
            artifact_records = raw.get("artifacts")
            if not isinstance(artifact_records, list):
                raise ClosureBuilderError(f"{task_id}: wheel lock is absent")
            for artifact in artifact_records:
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(f"{task_id}: wheel lock is malformed")
                filename = artifact.get("filename")
                digest = artifact.get("sha256")
                if not isinstance(filename, str) or not isinstance(digest, str):
                    raise ClosureBuilderError(f"{task_id}: wheel identity is malformed")
                source = (wheel_source / filename).resolve(strict=True)
                if (
                    source.parent != wheel_source.resolve()
                    or _sha256_file(source) != digest
                ):
                    raise ClosureBuilderError(
                        f"{task_id}: wheel artifact changed: {filename}"
                    )
                shutil.copyfile(source, wheel_target / filename)
            rendered = _render_closure_dockerfile(
                base_tag=base_tag,
                requirements=requirements,
                task_id=task_id,
                lock_digest=str(lock["lock_digest"]),
            )
        elif strategy == "content-locked-artifacts":
            artifact_records = raw.get("artifacts")
            apt_records = raw.get("apt_artifacts")
            runtime_records = raw.get("runtime_artifacts")
            acquisitions = raw.get("acquisitions")
            python_runtime = raw.get("python_runtime")
            indexes = raw.get("pypi_indexes")
            if (
                not isinstance(artifact_records, list)
                or not isinstance(apt_records, list)
                or not isinstance(runtime_records, list)
                or not isinstance(acquisitions, list)
                or not isinstance(python_runtime, Mapping)
                or not isinstance(indexes, list)
                or not all(isinstance(index, str) for index in indexes)
            ):
                raise ClosureBuilderError(f"{task_id}: content lock is malformed")
            wheel_source = lock_root / "artifact-cache" / "wheels" / task_id
            wheel_target = context / "wheels"
            wheel_target.mkdir(mode=0o755)
            for artifact in artifact_records:
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(f"{task_id}: wheel lock is malformed")
                filename = artifact.get("filename")
                digest = artifact.get("sha256")
                if not isinstance(filename, str) or not isinstance(digest, str):
                    raise ClosureBuilderError(f"{task_id}: wheel identity is malformed")
                source = (wheel_source / filename).resolve(strict=True)
                if (
                    source.parent != wheel_source.resolve()
                    or _sha256_file(source) != digest
                ):
                    raise ClosureBuilderError(
                        f"{task_id}: wheel artifact changed: {filename}"
                    )
                shutil.copyfile(source, wheel_target / filename)

            deb_target = context / "debs"
            deb_target.mkdir(mode=0o755)
            for artifact in apt_records:
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: apt content record is malformed"
                    )
                source = _validate_cache_file(
                    lock_root,
                    {
                        **dict(artifact),
                        "path": (
                            Path("artifact-cache")
                            / "apt"
                            / task_id
                            / str(artifact.get("filename"))
                        ).as_posix(),
                    },
                    f"{task_id}/apt:{artifact.get('filename')}",
                )
                shutil.copyfile(source, deb_target / source.name)

            runtime_target = context / "runtime"
            runtime_target.mkdir(mode=0o755)
            for index, artifact in enumerate(runtime_records):
                if not isinstance(artifact, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: runtime content record is malformed"
                    )
                source = _validate_cache_file(
                    lock_root, artifact, f"{task_id}/runtime-{index}"
                )
                if artifact.get("kind") == "uv-binary":
                    destination = runtime_target / "uv"
                elif artifact.get("kind") == "uvx-binary":
                    destination = runtime_target / "uvx"
                elif artifact.get("provider") == "locked-uv-installation":
                    destination = runtime_target / "python-installations.tar"
                else:
                    raise ClosureBuilderError(f"{task_id}: unknown runtime artifact")
                shutil.copyfile(source, destination)

            input_target = context / "inputs"
            input_target.mkdir(mode=0o755)
            normalized_acquisitions: list[Mapping[str, object]] = []
            for index, acquisition in enumerate(acquisitions):
                if not isinstance(acquisition, Mapping):
                    raise ClosureBuilderError(
                        f"{task_id}: acquisition record is malformed"
                    )
                source = _validate_cache_file(
                    lock_root, acquisition, f"{task_id}/acquisition-{index}"
                )
                destination = input_target / source.name
                if destination.exists():
                    raise ClosureBuilderError(
                        f"{task_id}: duplicate acquisition filename: {source.name}"
                    )
                shutil.copyfile(source, destination)
                normalized = dict(acquisition)
                normalized["path"] = source.name
                normalized_acquisitions.append(MappingProxyType(normalized))

            profile_name = raw.get("profile")
            python_selector = raw.get("python_selector")
            uv_mode = raw.get("uv_mode")
            index_strategy = raw.get("pypi_index_strategy")
            if not all(
                isinstance(value, str)
                for value in (
                    profile_name,
                    python_selector,
                    uv_mode,
                    index_strategy,
                )
            ):
                raise ClosureBuilderError(f"{task_id}: build recipe is malformed")
            rendered = _render_artifact_closure_dockerfile(
                base_tag=base_tag,
                task_id=task_id,
                profile_name=str(profile_name),
                lock_digest=str(lock["lock_digest"]),
                requirements=requirements,
                apt_artifacts=tuple(
                    item for item in apt_records if isinstance(item, Mapping)
                ),
                runtime_artifacts=tuple(
                    item for item in runtime_records if isinstance(item, Mapping)
                ),
                python_runtime=python_runtime,
                python_selector=str(python_selector),
                uv_mode=str(uv_mode),
                pypi_indexes=tuple(str(index) for index in indexes),
                pypi_index_strategy=str(index_strategy),
                acquisitions=tuple(normalized_acquisitions),
            )
        else:
            raise ClosureBuilderError(f"{task_id}: unknown build strategy")
        dockerfile.write_text(rendered, "utf-8")
        closure_tag = (
            f"rsi-arena/tb21-verifier-{task_id}:{str(lock['lock_digest'])[7:19]}"
        )
        _run_task_artifact(
            _docker(
                arguments.docker,
                arguments.docker_host,
                "build",
                "--network=none",
                "--pull=false",
                "--no-cache",
                "--tag",
                closure_tag,
                str(context),
            ),
            task_id=task_id,
            profile=str(raw.get("profile")),
            artifact="verifier-only-image",
            timeout=1800.0,
        )
        closure = _inspect_image(arguments.docker, arguments.docker_host, closure_tag)
        closure_id = str(closure["Id"])
        source = lock_root / "task-sources" / task_id
        if task_tree_digest(source) != raw.get("task_material_digest"):
            raise ClosureBuilderError(
                f"{task_id}/{raw.get('profile')}/task-source: content changed"
            )
        copied_inputs = output / "inputs" / task_id
        if copied_inputs.exists():
            shutil.rmtree(copied_inputs)
        shutil.copytree(source, copied_inputs, symlinks=True)
        build_input = {
            "task_lock": dict(raw),
            "dockerfile_sha256": _sha256_file(dockerfile),
            "lock_digest": lock["lock_digest"],
        }
        built_task = {
            "family": raw["family"],
            "profile": raw["profile"],
            "task_material_digest": raw["task_material_digest"],
            "test_digest": raw["test_digest"],
            "build_input_digest": _canonical_digest(build_input),
            "configured_base_image": raw["configured_base_image"],
            "closure_base_image_id": base_id,
            "closure_image_id": closure_id,
            "dynamic_candidate_uv": raw["dynamic_candidate_uv"],
            "artifact_set_digest": raw["artifact_set_digest"],
            "offline_smoke": None,
        }
        built[task_id] = built_task
        _atomic_json(
            build_fragment_path,
            {
                "schema": AUTHORITY_SCHEMA,
                "task_id": task_id,
                "lock_digest": lock["lock_digest"],
                "task": built_task,
            },
        )
        _write_state(output, phase="building", task_ids=tuple(built))
    task_source = lock["task_source"]
    assert isinstance(task_source, Mapping)
    authority = _builder_authority_payload(
        task_source=task_source,
        lock_digest=lock["lock_digest"],
        platform=lock["platform"],
        tasks={
            task_id: value
            for task_id, value in built.items()
            if isinstance(value, Mapping)
        },
    )
    _atomic_json(authority_path, authority)
    _decode_builder_authority(authority_path, require_verified=False)
    _write_state(output, phase="built", task_ids=task_ids)
    return authority_path


@dataclass
class _StaticCandidateEnvironment:
    source: Path

    async def stop_service(self, name: str) -> None:
        if name != "main":
            raise ClosureBuilderError("unexpected no-model service")

    async def download_dir(self, remote: str, destination: Path) -> None:
        if remote != "/app":
            raise ClosureBuilderError("unexpected no-model candidate path")
        shutil.copytree(self.source, destination, dirs_exist_ok=True, symlinks=True)


def _pending_authority(
    path: Path, payload: Mapping[str, object], task_id: str
) -> OfflineClosureAuthority:
    raw = payload["tasks"][task_id]
    assert isinstance(raw, Mapping)
    source = payload["task_source"]
    assert isinstance(source, Mapping)
    binding = OfflineTaskBinding(
        task_id=task_id,
        family=str(raw["family"]),
        closure_image_id=str(raw["closure_image_id"]),
        closure_base_image_id=str(raw["closure_base_image_id"]),
        dynamic_candidate_uv=bool(raw["dynamic_candidate_uv"]),
        profile=str(raw["profile"]),
        test_digest=str(raw["test_digest"]),
        task_material_digest=str(raw["task_material_digest"]),
        build_input_digest=str(raw["build_input_digest"]),
    )
    snapshot_digest = _canonical_digest(
        {"task_source": dict(source), "task_id": task_id}
    )
    return OfflineClosureAuthority(
        path=path,
        digest=_sha256_file(path),
        content_digest=str(payload["authority_digest"]),
        lock_digest=str(payload["lock_digest"]),
        snapshot_digest=snapshot_digest,
        source_commit=str(source["commit"]),
        source_tree=str(source["repository_tree"]),
        tasks_tree=str(source["tasks_tree"]),
        closure_digest=str(payload["closure_digest"]),
        tasks=MappingProxyType({task_id: binding}),
        stage="verifying",
        payload=MappingProxyType(dict(payload)),
    )


def _extract_candidate(
    *, docker: str, docker_host: str, image_id: str, destination: Path
) -> None:
    image = _inspect_image(docker, docker_host, image_id)
    config = image.get("Config")
    working_directory = (
        config.get("WorkingDir") if isinstance(config, Mapping) else None
    )
    source = PurePosixPath(working_directory or "/app")
    if (
        not source.is_absolute()
        or source == PurePosixPath("/")
        or any(part in {"", ".", ".."} for part in source.parts[1:])
    ):
        raise ClosureBuilderError("no-model base working directory is invalid")
    container = _run(
        _docker(docker, docker_host, "create", image_id), timeout=120.0
    ).stdout.strip()
    if not container:
        raise ClosureBuilderError("no-model base container was not created")
    try:
        destination.mkdir(mode=0o700, parents=True)
        _run(
            _docker(
                docker,
                docker_host,
                "cp",
                f"{container}:{source.as_posix()}/.",
                str(destination),
            ),
            timeout=300.0,
        )
    finally:
        _run(
            _docker(docker, docker_host, "rm", "--force", container),
            timeout=120.0,
            check=False,
        )


def _prepare_reshard_no_model_candidate(
    *,
    docker: str,
    docker_host: str,
    image_id: str,
    candidate: Path,
    task_input: Path,
) -> None:
    solution = task_input / "solution" / "solve.sh"
    if not solution.is_file():
        raise ClosureBuilderError("reshard-c4-data: official solution is absent")
    _run_task_artifact(
        _docker(
            docker,
            docker_host,
            "run",
            "--rm",
            "--pull=never",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={candidate},dst=/app",
            "--mount",
            f"type=bind,src={solution},dst=/solution.sh,readonly",
            "--entrypoint",
            "/bin/bash",
            image_id,
            "/solution.sh",
        ),
        task_id="reshard-c4-data",
        profile="candidate-uv",
        artifact="official-solution-candidate",
        timeout=300.0,
    )
    (candidate / "pyproject.toml").write_text(
        "[project]\n"
        'name = "tb21-reshard-offline-smoke"\n'
        'version = "0.0.0"\n'
        'requires-python = ">=3.13"\n'
        'dependencies = ["tqdm==4.67.1"]\n',
        "utf-8",
    )


async def _verify_one(
    *,
    authority_path: Path,
    payload: Mapping[str, object],
    task_id: str,
    docker: str,
    docker_host: str,
    verifier_timeout_multiplier: float,
) -> Mapping[str, object]:
    authority = _pending_authority(authority_path, payload, task_id)
    binding = authority.binding(task_id)
    _inspect_image(docker, docker_host, binding.closure_base_image_id)
    _inspect_image(docker, docker_host, binding.closure_image_id)
    verify_root = authority_path.parent / "offline-smoke" / task_id
    if verify_root.exists():
        shutil.rmtree(verify_root)
    candidate = verify_root / "candidate-app"
    _extract_candidate(
        docker=docker,
        docker_host=docker_host,
        image_id=binding.closure_base_image_id,
        destination=candidate,
    )
    task_input = authority_path.parent / "inputs" / task_id
    try:
        task_manifest = tomllib.loads((task_input / "task.toml").read_text("utf-8"))
        native_verifier_timeout = float(
            task_manifest.get("verifier", {}).get("timeout_sec", 900.0)
        )
    except (OSError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
        raise ClosureBuilderError(
            f"{task_id}: verifier timeout authority is invalid"
        ) from exc
    verifier_timeout_seconds = native_verifier_timeout * float(
        verifier_timeout_multiplier
    )
    if not math.isfinite(verifier_timeout_seconds) or verifier_timeout_seconds <= 0.0:
        raise ClosureBuilderError(f"{task_id}: verifier timeout multiplier is invalid")
    if binding.dynamic_candidate_uv:
        _prepare_reshard_no_model_candidate(
            docker=docker,
            docker_host=docker_host,
            image_id=binding.closure_base_image_id,
            candidate=candidate,
            task_input=task_input,
        )
    tests = task_input / "tests"
    trial = verify_root / "trial"
    run_id = "tb21-harbor-cell-" + uuid.uuid4().hex
    docker_path = Path(shutil.which(docker) or docker).resolve(strict=True)
    kwargs = offline_verifier_launch_kwargs(
        authority=authority,
        source_commit=authority.source_commit,
        task_id=task_id,
        run_id=run_id,
        expected_agent_image_id=binding.closure_base_image_id,
        shared_run_root=verify_root / "runtime",
        tests_path=tests,
        docker=docker_path,
        docker_host=docker_host,
        verifier_timeout_seconds=verifier_timeout_seconds,
    )
    verifier = TB21OfflineVerifier(
        **dict(kwargs),
        environment=_StaticCandidateEnvironment(candidate),
        trial_paths=SimpleNamespace(verifier_dir=trial / "verifier"),
        verifier_env={},
    )
    result = await verifier.verify()
    verifier_dir = trial / "verifier"
    ctrf = _load_json(verifier_dir / "ctrf.json")
    _load_json(verifier_dir / "offline-verifier-provenance.json")
    reward = float(result.rewards["reward"])
    return MappingProxyType(
        {
            "status": "verified",
            "verified_at": _utc_now(),
            "network_mode": "none",
            "base_image_id": binding.closure_base_image_id,
            "closure_image_id": binding.closure_image_id,
            "candidate_snapshot": dict(canonical_app_identity(candidate)),
            "ctrf": ctrf.get("results", {}).get("summary", {}),
            "ctrf_sha256": _sha256_file(verifier_dir / "ctrf.json"),
            "reward": reward,
            "reward_sha256": _sha256_file(verifier_dir / "reward.txt"),
            "provenance_sha256": _sha256_file(
                verifier_dir / "offline-verifier-provenance.json"
            ),
            "original_test_digest": binding.test_digest,
            "build_input_digest": binding.build_input_digest,
        }
    )


def _refresh_authority_verification(authority: dict[str, Any]) -> tuple[str, ...]:
    """Refresh the canonical stage, evidence digest, and content digest."""

    verified_task_ids = tuple(
        sorted(
            task_id
            for task_id, raw in authority["tasks"].items()
            if isinstance(raw, Mapping)
            and isinstance(raw.get("offline_smoke"), Mapping)
            and raw["offline_smoke"].get("status") == "verified"
        )
    )
    all_task_ids = tuple(sorted(authority["tasks"]))
    evidence = {
        task_id: authority["tasks"][task_id]["offline_smoke"]
        for task_id in verified_task_ids
    }
    if not verified_task_ids:
        verification: dict[str, object] = {
            "status": "built",
            "network_mode": "none",
            "task_ids": [],
        }
    elif verified_task_ids == all_task_ids:
        verification = {
            "status": "verified",
            "network_mode": "none",
            "task_ids": list(all_task_ids),
            "evidence_digest": _canonical_digest(evidence),
        }
    else:
        verification = {
            "status": "verifying",
            "network_mode": "none",
            "task_ids": list(verified_task_ids),
            "evidence_digest": _canonical_digest(evidence),
        }
    authority["verification"] = verification
    authority.pop("authority_digest", None)
    authority["authority_digest"] = _canonical_digest(authority)
    return verified_task_ids


def _persist_verification_progress(
    authority_path: Path, authority: dict[str, Any]
) -> tuple[str, ...]:
    verified_task_ids = _refresh_authority_verification(authority)
    all_task_ids = tuple(sorted(authority["tasks"]))
    _atomic_json(authority_path, authority)
    _write_state(
        authority_path.parent,
        phase=("verified" if verified_task_ids == all_task_ids else "verifying"),
        task_ids=verified_task_ids,
    )
    return verified_task_ids


def _verify_command(arguments: argparse.Namespace) -> Path:
    authority_path = Path(arguments.authority).expanduser().resolve(strict=True)
    decoded = _decode_builder_authority(authority_path, require_verified=False)
    authority = dict(decoded.payload)
    task_ids = _selected_locked_tasks(authority, arguments.task)
    if decoded.stage == "verified":
        return authority_path
    for task_id in task_ids:
        raw_task = authority["tasks"][task_id]
        assert isinstance(raw_task, dict)
        existing = raw_task.get("offline_smoke")
        if not (
            isinstance(existing, Mapping)
            and existing.get("status") == "verified"
            and existing.get("network_mode") == "none"
            and existing.get("base_image_id") == raw_task.get("closure_base_image_id")
            and existing.get("closure_image_id") == raw_task.get("closure_image_id")
        ):
            raw_task["offline_smoke"] = dict(
                asyncio.run(
                    _verify_one(
                        authority_path=authority_path,
                        payload=authority,
                        task_id=task_id,
                        docker=arguments.docker,
                        docker_host=arguments.docker_host,
                        verifier_timeout_multiplier=(
                            arguments.verifier_timeout_multiplier
                        ),
                    )
                )
            )
            _persist_verification_progress(authority_path, authority)
            _decode_builder_authority(authority_path, require_verified=False)
    _persist_verification_progress(authority_path, authority)
    _decode_builder_authority(authority_path, require_verified=True)
    return authority_path


def _reuse_prepared_authority(
    arguments: argparse.Namespace, cache_root: Path
) -> Path | None:
    """Reuse a complete derived authority without rehashing dormant build inputs."""

    authority_path = cache_root / "build" / "offline-verifier-authority.json"
    if not authority_path.is_file():
        return None
    decoded = _decode_builder_authority(authority_path, require_verified=False)
    authority = dict(decoded.payload)
    census = _load_json(VERIFIER_DEPENDENCY_CLOSURE_PATH)
    selected = _select_tasks(census, arguments.task, arguments.family)
    if tuple(sorted(authority["tasks"])) != selected:
        raise ClosureBuilderError(
            "existing verified authority selected a different task set"
        )

    if decoded.stage != "verified":
        return None

    repository = Path(arguments.task_repository).expanduser().resolve(strict=True)
    snapshot = load_tb21_task_snapshot(repository)
    if _git(repository, "rev-parse", "HEAD") != snapshot.commit:
        raise ClosureBuilderError("task repository HEAD is not the pinned TB2.1 commit")
    if _git(repository, "status", "--porcelain=v1"):
        raise ClosureBuilderError("task repository must be clean before reuse")
    expected_source = {
        "repository": "harbor-framework/terminal-bench-2-1",
        "commit": snapshot.commit,
        "repository_tree": snapshot.repository_tree,
        "tasks_tree": snapshot.tasks_tree,
    }
    if authority.get("task_source") != expected_source:
        raise ClosureBuilderError("verified authority task source changed identity")
    try:
        load_offline_closure_authority(snapshot, authority_path=authority_path)
    except TB21OfflineVerifierError as exc:
        raise ClosureBuilderError(
            "verified authority no longer matches the runtime task snapshot"
        ) from exc

    image_ids = {
        image_id
        for binding in decoded.tasks.values()
        for image_id in (
            binding.closure_base_image_id,
            binding.closure_image_id,
        )
    }
    try:
        for image_id in sorted(image_ids):
            _inspect_image(arguments.docker, arguments.docker_host, image_id)
    except ClosureBuilderError:
        # The durable lock artifacts can reconstruct an evicted derived image.
        return None
    _ensure_configured_base_tags(
        authority,
        docker=arguments.docker,
        docker_host=arguments.docker_host,
    )
    return authority_path


def _ensure_configured_base_tags(
    authority: Mapping[str, object], *, docker: str, docker_host: str
) -> None:
    """Restore task-manifest image references from exact authority image IDs."""

    tasks = authority.get("tasks")
    if not isinstance(tasks, Mapping):
        raise ClosureBuilderError("offline verifier authority tasks are invalid")
    for task_id, raw in sorted(tasks.items()):
        if not isinstance(raw, Mapping):
            raise ClosureBuilderError(f"{task_id}: authority binding is invalid")
        image_ref = raw.get("configured_base_image")
        image_id = raw.get("closure_base_image_id")
        if not isinstance(image_ref, str) or not isinstance(image_id, str):
            raise ClosureBuilderError(
                f"{task_id}: configured base image binding is absent"
            )
        try:
            observed = _inspect_image(docker, docker_host, image_ref)
        except ClosureBuilderError:
            _run(
                _docker(docker, docker_host, "tag", image_id, image_ref),
                timeout=60.0,
            )
            observed = _inspect_image(docker, docker_host, image_ref)
        if observed.get("Id") != image_id:
            raise ClosureBuilderError(
                f"{task_id}: configured base image tag changed authority"
            )


def _prepare_command(arguments: argparse.Namespace) -> Path:
    """Ensure one reusable source-locked closure exists for this benchmark."""

    cache_root = _require_output_root(Path(arguments.cache_dir))
    if arguments.temp_dir:
        temporary_root = _require_output_root(Path(arguments.temp_dir)).resolve()
        os.environ["TMPDIR"] = str(temporary_root)
        tempfile.tempdir = str(temporary_root)
    reused = _reuse_prepared_authority(arguments, cache_root)
    if reused is not None:
        return reused
    lock_path = _lock_command(
        argparse.Namespace(
            task_repository=arguments.task_repository,
            output_dir=str(cache_root / "lock"),
            task=arguments.task,
            family=arguments.family,
            docker=arguments.docker,
            docker_host=arguments.docker_host,
        )
    )
    authority_path = _build_command(
        argparse.Namespace(
            lock=str(lock_path),
            output_dir=str(cache_root / "build"),
            task=[],
            docker=arguments.docker,
            docker_host=arguments.docker_host,
        )
    )
    authority_path = _verify_command(
        argparse.Namespace(
            authority=str(authority_path),
            task=[],
            docker=arguments.docker,
            docker_host=arguments.docker_host,
            verifier_timeout_multiplier=arguments.verifier_timeout_multiplier,
        )
    )
    authority = _load_and_validate_authority(authority_path)
    _ensure_configured_base_tags(
        authority,
        docker=arguments.docker,
        docker_host=arguments.docker_host,
    )
    return authority_path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build TB2.1 verifier-only closure images from content locks"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser(
        "prepare",
        help="lock, build, verify, or reuse one benchmark closure cache",
    )
    prepare.add_argument("--task-repository", required=True)
    prepare.add_argument("--cache-dir", required=True)
    prepare.add_argument(
        "--temp-dir",
        help="large local temporary directory inherited by resolver subprocesses",
    )
    prepare.add_argument("--verifier-timeout-multiplier", required=True, type=float)
    prepare.add_argument(
        "--task", action="append", default=[], help="task ID (repeatable)"
    )
    prepare.add_argument(
        "--family",
        action="append",
        default=[],
        help="public dependency family or profile ID (repeatable)",
    )
    prepare.add_argument("--docker", default="docker")
    prepare.add_argument(
        "--docker-host",
        default=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
    )
    prepare.set_defaults(handler=_prepare_command)

    lock = subparsers.add_parser("lock", help="resolve and fetch online inputs")
    lock.add_argument("--task-repository", required=True)
    lock.add_argument("--output-dir", required=True)
    lock.add_argument(
        "--task", action="append", default=[], help="task ID (repeatable)"
    )
    lock.add_argument(
        "--family",
        action="append",
        default=[],
        help="public dependency family or profile ID (repeatable)",
    )
    lock.add_argument("--docker", default="docker")
    lock.add_argument(
        "--docker-host",
        default=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
    )
    lock.set_defaults(handler=_lock_command)

    build = subparsers.add_parser("build", help="build with network disabled")
    build.add_argument("--lock", required=True)
    build.add_argument("--output-dir", required=True)
    build.add_argument("--task", action="append", default=[])
    build.add_argument("--docker", default="docker")
    build.add_argument(
        "--docker-host",
        default=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
    )
    build.set_defaults(handler=_build_command)

    verify = subparsers.add_parser(
        "verify", help="run the original test.sh with --network none"
    )
    verify.add_argument("--authority", required=True)
    verify.add_argument("--task", action="append", default=[])
    verify.add_argument("--docker", default="docker")
    verify.add_argument("--verifier-timeout-multiplier", required=True, type=float)
    verify.add_argument(
        "--docker-host",
        default=os.environ.get("DOCKER_HOST", "unix:///var/run/docker.sock"),
    )
    verify.set_defaults(handler=_verify_command)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    arguments = parser.parse_args(argv)
    try:
        result = arguments.handler(arguments)
    except ClosureBuilderError as exc:
        parser.exit(2, f"closure builder error: {exc}\n")
    print(result)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


__all__ = [
    "AUTHORITY_SCHEMA",
    "ClosureBuilderError",
    "LOCK_SCHEMA",
    "load_verified_exact_image_closure",
    "main",
]
