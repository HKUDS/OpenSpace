"""Transfer the exact images in a verified TB2.1 builder authority.

The original dirty-worktree tool audited content-addressed local Docker image
IDs, exported exact images from a daemon that still owned them, and verified
that a destination daemon reconstructed the same IDs after ``docker load``.

The tool resolves image identities only through the canonical closure builder
authority.  Authority validation completes before any Docker command runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rsi_arena.benchmarks.tb21.closure_builder import (
    ClosureBuilderError,
    load_verified_exact_image_closure,
)

SCHEMA = "tb21-offline-verifier-image-bundle"
DEFAULT_TAG_PREFIX = "rsi-arena/tb21-offline-verifier"
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_TASK_RE = re.compile(r"[a-z0-9][a-z0-9._-]*")
_TAG_PREFIX_RE = re.compile(
    r"[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*"
)


class ClosureBundleError(RuntimeError):
    """The image closure or transfer bundle is incomplete."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ClosureBundleError(f"{label} is not a JSON object")
    return value


def _image_id(value: object, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ClosureBundleError(f"{label} is not a sha256 Docker image ID")
    return value


def _load_json(path: Path, label: str) -> Mapping[str, Any]:
    try:
        return _mapping(json.loads(path.read_text("utf-8")), label)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ClosureBundleError(f"cannot load {label}: {path}") from exc


def _authority_images(path: Path) -> tuple[Mapping[str, Any], dict[str, Any]]:
    try:
        authority = load_verified_exact_image_closure(path)
    except ClosureBuilderError as exc:
        raise ClosureBundleError("TB2 exact-image authority is invalid") from exc
    raw_images = authority.get("images")
    if not isinstance(raw_images, Mapping):  # pragma: no cover - builder contract
        raise ClosureBundleError("TB2 exact-image authority has no image closure")
    images: dict[str, Any] = {}
    for task_id, raw_pair in raw_images.items():
        if not isinstance(task_id, str) or not isinstance(raw_pair, Mapping):
            raise ClosureBundleError("TB2 exact-image authority is malformed")
        images[task_id] = {
            "base": _image_id(raw_pair.get("base"), f"{task_id} base"),
            "closure": _image_id(raw_pair.get("closure"), f"{task_id} closure"),
        }
    if not images:
        raise ClosureBundleError("TB2 exact-image authority is empty")
    return authority, images


def _authority_identity(authority: Mapping[str, Any]) -> dict[str, Any]:
    source = _mapping(authority.get("task_source"), "authority.task_source")
    task_ids = authority.get("task_ids")
    if not isinstance(task_ids, tuple) or not all(
        isinstance(task_id, str) for task_id in task_ids
    ):
        raise ClosureBundleError("authority task identity is invalid")
    return {
        "schema": authority.get("schema"),
        "file_sha256": authority.get("file_digest"),
        "content_digest": authority.get("content_digest"),
        "lock_digest": authority.get("lock_digest"),
        "closure_digest": authority.get("closure_digest"),
        "task_source": dict(source),
        "task_ids": list(task_ids),
    }


def _docker(
    executable: str,
    host: str,
    args: Sequence[str],
    *,
    timeout: float | None = 120.0,
) -> subprocess.CompletedProcess[str]:
    command = [executable, "--host", host, *args]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClosureBundleError(f"Docker command did not complete: {args[0]}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-1200:]
        raise ClosureBundleError(
            f"Docker command failed ({completed.returncode}): {args[0]}: {detail}"
        )
    return completed


def _inspect(
    executable: str, host: str, reference: str, *, required: bool
) -> str | None:
    command = [
        executable,
        "--host",
        host,
        "image",
        "inspect",
        reference,
        "--format",
        "{{.Id}}",
    ]
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=60.0, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ClosureBundleError("Docker image inspection did not complete") from exc
    if completed.returncode != 0:
        if required:
            detail = (completed.stderr or completed.stdout).strip()[-1200:]
            raise ClosureBundleError(
                f"Docker image is unavailable: {reference}: {detail}"
            )
        return None
    return _image_id(completed.stdout.strip(), f"observed image {reference}")


def _audit(
    executable: str, host: str, images: Mapping[str, Mapping[str, str]]
) -> dict[str, Any]:
    missing: list[dict[str, str]] = []
    mismatched: list[dict[str, str]] = []
    found = {"base": 0, "closure": 0}
    for task_id, pair in images.items():
        for role in ("base", "closure"):
            expected = pair[role]
            observed = _inspect(executable, host, expected, required=False)
            if observed is None:
                missing.append({"task_id": task_id, "role": role, "image_id": expected})
            elif observed != expected:
                mismatched.append(
                    {
                        "task_id": task_id,
                        "role": role,
                        "expected": expected,
                        "observed": observed,
                    }
                )
            else:
                found[role] += 1
    return {
        "docker_host": host,
        "task_count": len(images),
        "expected": {"base": len(images), "closure": len(images)},
        "found": found,
        "missing": missing,
        "mismatched": mismatched,
        "complete": not missing and not mismatched,
    }


def _tag_prefix(value: str) -> str:
    if not _TAG_PREFIX_RE.fullmatch(value):
        raise ClosureBundleError("tag prefix is not a lowercase Docker repository")
    return value


def _tagged_images(
    images: Mapping[str, Mapping[str, str]], tag_prefix: str
) -> dict[str, Any]:
    return {
        task_id: {
            role: {
                "image_id": pair[role],
                "reference": f"{tag_prefix}/{task_id}:{role}",
            }
            for role in ("base", "closure")
        }
        for task_id, pair in images.items()
    }


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.partial-{os.getpid()}")
    if temporary.exists():
        raise ClosureBundleError(f"temporary output already exists: {temporary}")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n", "utf-8"
        )
        temporary.replace(path)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def command_audit(args: argparse.Namespace) -> int:
    authority_path = Path(args.authority).expanduser()
    authority, images = _authority_images(authority_path)
    report = _audit(args.docker, args.docker_host, images)
    report["authority"] = str(authority_path)
    report["authority_identity"] = _authority_identity(authority)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["complete"] else 2


def command_export(args: argparse.Namespace) -> int:
    authority_path = Path(args.authority).expanduser()
    authority, images = _authority_images(authority_path)
    report = _audit(args.docker, args.docker_host, images)
    if not report["complete"]:
        raise ClosureBundleError(
            "source Docker daemon does not contain the complete pinned closure"
        )

    output = Path(args.output).resolve()
    manifest_path = Path(args.manifest).resolve()
    if output.exists() or manifest_path.exists():
        raise ClosureBundleError("bundle output or manifest already exists")
    output.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    partial = output.with_name(f".{output.name}.partial-{os.getpid()}")
    if partial.exists():
        raise ClosureBundleError(f"temporary output already exists: {partial}")

    prefix = _tag_prefix(args.tag_prefix)
    tagged = _tagged_images(images, prefix)
    conflicts: list[str] = []
    for task_id in sorted(tagged):
        for role in ("base", "closure"):
            record = tagged[task_id][role]
            observed = _inspect(
                args.docker, args.docker_host, record["reference"], required=False
            )
            if observed is not None and observed != record["image_id"]:
                conflicts.append(
                    f"{record['reference']}: expected {record['image_id']}, "
                    f"observed {observed}"
                )
    if conflicts:
        raise ClosureBundleError(
            "bundle tag prefix conflicts with existing images:\n" + "\n".join(conflicts)
        )

    references: list[str] = []
    for task_id in sorted(tagged):
        for role in ("base", "closure"):
            record = tagged[task_id][role]
            _docker(
                args.docker,
                args.docker_host,
                ("image", "tag", record["image_id"], record["reference"]),
            )
            observed = _inspect(
                args.docker, args.docker_host, record["reference"], required=True
            )
            if observed != record["image_id"]:
                raise ClosureBundleError(
                    f"tag changed image identity: {record['reference']}"
                )
            references.append(record["reference"])

    try:
        _docker(
            args.docker,
            args.docker_host,
            ("image", "save", "--output", str(partial), *references),
            timeout=None,
        )
        partial.replace(output)
    except Exception:
        partial.unlink(missing_ok=True)
        raise

    manifest = {
        "schema": SCHEMA,
        "authority": _authority_identity(authority),
        "archive": output.name,
        "archive_sha256": _sha256_file(output),
        "tag_prefix": prefix,
        "task_count": len(images),
        "image_count": len(references),
        "tasks": tagged,
    }
    _write_json_atomic(manifest_path, manifest)
    print(
        json.dumps(
            {"bundle": str(output), "manifest": str(manifest_path), **report},
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _manifest_images(manifest: Mapping[str, Any]) -> dict[str, Any]:
    task_count = manifest.get("task_count")
    image_count = manifest.get("image_count")
    if (
        manifest.get("schema") != SCHEMA
        or not isinstance(task_count, int)
        or isinstance(task_count, bool)
        or task_count < 1
        or image_count != task_count * 2
    ):
        raise ClosureBundleError("bundle manifest schema or task count is invalid")
    raw_tasks = _mapping(manifest.get("tasks"), "manifest.tasks")
    if len(raw_tasks) != task_count:
        raise ClosureBundleError("bundle manifest task inventory is incomplete")
    images: dict[str, Any] = {}
    for task_id, raw_pair in sorted(raw_tasks.items()):
        if not isinstance(task_id, str) or not _TASK_RE.fullmatch(task_id):
            raise ClosureBundleError(f"invalid task ID in bundle: {task_id!r}")
        pair = _mapping(raw_pair, f"manifest.tasks.{task_id}")
        if set(pair) != {"base", "closure"}:
            raise ClosureBundleError(f"bundle task roles are incomplete: {task_id}")
        images[task_id] = {}
        for role in ("base", "closure"):
            record = _mapping(pair[role], f"manifest.tasks.{task_id}.{role}")
            if set(record) != {"image_id", "reference"}:
                raise ClosureBundleError(
                    f"bundle image record is invalid: {task_id}:{role}"
                )
            reference = record["reference"]
            if not isinstance(reference, str) or not reference:
                raise ClosureBundleError(
                    f"bundle image reference is invalid: {task_id}:{role}"
                )
            images[task_id][role] = {
                "image_id": _image_id(record["image_id"], f"{task_id} {role}"),
                "reference": reference,
            }
    return images


def command_import(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).resolve(strict=True)
    manifest_path = Path(args.manifest).resolve(strict=True)
    authority_path = Path(args.authority).expanduser()
    authority, authority_images = _authority_images(authority_path)
    manifest = _load_json(manifest_path, "bundle manifest")
    tagged = _manifest_images(manifest)
    if manifest.get("authority") != _authority_identity(authority):
        raise ClosureBundleError("bundle manifest does not match the local authority")
    if set(tagged) != set(authority_images):
        raise ClosureBundleError(
            "bundle manifest task inventory does not match the local authority"
        )
    for task_id, pair in authority_images.items():
        for role in ("base", "closure"):
            if tagged[task_id][role]["image_id"] != pair[role]:
                raise ClosureBundleError(
                    f"bundle image does not match authority: {task_id}:{role}"
                )
    if manifest.get("archive_sha256") != _sha256_file(bundle):
        raise ClosureBundleError("bundle archive digest does not match its manifest")

    _docker(
        args.docker,
        args.docker_host,
        ("image", "load", "--input", str(bundle)),
        timeout=None,
    )
    errors: list[str] = []
    for task_id in sorted(tagged):
        for role in ("base", "closure"):
            record = tagged[task_id][role]
            observed = _inspect(
                args.docker, args.docker_host, record["reference"], required=False
            )
            if observed != record["image_id"]:
                errors.append(
                    f"{task_id}:{role}: expected {record['image_id']}, "
                    f"observed {observed}"
                )
    if errors:
        raise ClosureBundleError(
            "loaded bundle did not reconstruct exact image IDs:\n" + "\n".join(errors)
        )
    print(
        json.dumps(
            {
                "status": "complete",
                "docker_host": args.docker_host,
                "task_count": len(tagged),
                "image_count": len(tagged) * 2,
                "archive_sha256": manifest["archive_sha256"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--docker", default="docker", help="Docker CLI executable")
    parser.add_argument(
        "--docker-host",
        required=True,
        help="explicit Docker daemon, for example unix:///path/to/docker.sock",
    )
    parser.add_argument(
        "--authority",
        required=True,
        help=("verified authority emitted by the active TB2 closure builder"),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit = subparsers.add_parser("audit", help="inspect all pinned image IDs")
    audit.set_defaults(handler=command_audit)

    export = subparsers.add_parser(
        "export", help="tag and save the exact closure as one portable archive"
    )
    export.add_argument("--output", required=True, help="new docker-save archive path")
    export.add_argument("--manifest", required=True, help="new sidecar manifest path")
    export.add_argument("--tag-prefix", default=DEFAULT_TAG_PREFIX)
    export.set_defaults(handler=command_export)

    import_parser = subparsers.add_parser(
        "import", help="load a bundle and verify every reconstructed image ID"
    )
    import_parser.add_argument("--bundle", required=True)
    import_parser.add_argument("--manifest", required=True)
    import_parser.set_defaults(handler=command_import)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (ClosureBundleError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
