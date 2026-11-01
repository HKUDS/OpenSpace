"""Scope / secret gate over a staged diff (orchestrator step 6).

Defense-in-depth: the harness sandbox flags are advisory; this gate is the
authoritative check before a candidate commit is created.
"""

from __future__ import annotations

import base64
import fnmatch
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from rsi_arena.contracts import ScopeSpec

# Deliberately conservative first cut; extend as taxonomy grows.
SECRET_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{20,}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (
        "generic_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|secret|password|token)\b\s*[:=]\s*['\"][^'\"]{16,}['\"]"
        ),
    ),
)

ALWAYS_PROTECTED = (".git/**", ".git")
ALLOWED_STAGED_MODES = frozenset({"100644", "100755"})
MAX_CHANGED_FILES = 100
MAX_STAGED_BLOB_BYTES = 1_000_000
MAX_STAGED_TOTAL_BYTES = 4_000_000


@dataclass(frozen=True)
class GateResult:
    ok: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    # fnmatch's '*' crosses '/' so 'src/**' and 'src/*' both cover subtrees;
    # acceptable for the gate (over-matching protected is fail-safe).
    return any(
        fnmatch.fnmatch(path, pattern) or path == pattern.rstrip("/*")
        for pattern in patterns
    )


def check_scope(changed_files: list[str], scope: ScopeSpec) -> GateResult:
    violations = []
    for path in changed_files:
        if _matches_any(path, ALWAYS_PROTECTED + scope.protected):
            violations.append(f"protected path modified: {path}")
        elif not _matches_any(path, scope.editable):
            violations.append(f"path outside editable scope: {path}")
    return GateResult(ok=not violations, violations=tuple(violations))


def _secret_variants(value: str) -> tuple[str, ...]:
    raw = value.encode("utf-8")
    variants = {value}
    if 4 <= len(raw) <= 4096:
        variants.update(
            {
                base64.b64encode(raw).decode("ascii"),
                base64.urlsafe_b64encode(raw).decode("ascii"),
                raw.hex(),
            }
        )
    return tuple(fragment for fragment in variants if len(fragment) >= 4)


def scan_secrets(
    diff_text: str, *, sensitive_literals: Iterable[str] = ()
) -> GateResult:
    violations = []
    added_lines = "\n".join(
        line[1:]
        for line in diff_text.splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for line in diff_text.splitlines():
        if not line.startswith("+"):
            continue  # only newly added content matters
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(line):
                violations.append(f"possible secret ({name}) in added line")
                break
    for value in sensitive_literals:
        if not isinstance(value, str) or not value:
            continue
        if any(fragment in added_lines for fragment in _secret_variants(value)):
            violations.append("injected credential material in staged content")
            break
    return GateResult(ok=not violations, violations=tuple(violations))


def _git_bytes(worktree: Path, args: list[str]) -> bytes:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(worktree),
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
        },
        capture_output=True,
    )
    if proc.returncode != 0:
        raise ValueError("cannot inspect staged Git objects")
    return proc.stdout


def inspect_staged_tree(worktree: Path, changed_files: list[str]) -> GateResult:
    """Reject object modes/content that a textual diff cannot inspect.

    Deleted files have no stage-0 object and remain admissible.  New or
    modified objects must be bounded UTF-8 regular files; symlinks, gitlinks,
    binary payloads and oversized blobs fail closed before commit creation.
    """
    violations: list[str] = []
    if len(changed_files) > MAX_CHANGED_FILES:
        violations.append("too many files in candidate change")
        return GateResult(ok=False, violations=tuple(violations))

    total = 0
    for path in changed_files:
        if (
            not path
            or len(path) > 512
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
            or Path(path).is_absolute()
            or any(part in {"", ".", ".."} for part in Path(path).parts)
        ):
            violations.append("candidate contains an unsafe path name")
            continue
        record = _git_bytes(worktree, ["ls-files", "--stage", "-z", "--", path])
        if not record:
            continue  # deletion
        rows = [row for row in record.split(b"\0") if row]
        if len(rows) != 1:
            violations.append(f"ambiguous staged object: {path}")
            continue
        try:
            header, encoded_path = rows[0].split(b"\t", 1)
            mode, object_id, stage = header.decode("ascii").split(" ")
            recorded_path = os.fsdecode(encoded_path)
        except (ValueError, UnicodeError):
            violations.append("malformed staged Git object")
            continue
        if recorded_path != path or stage != "0":
            violations.append(f"ambiguous staged object: {path}")
            continue
        if mode not in ALLOWED_STAGED_MODES:
            violations.append(f"non-regular staged object forbidden: {path}")
            continue
        blob = _git_bytes(worktree, ["cat-file", "blob", object_id])
        total += len(blob)
        if len(blob) > MAX_STAGED_BLOB_BYTES:
            violations.append(f"staged file exceeds size limit: {path}")
            continue
        if b"\0" in blob:
            violations.append(f"binary staged file forbidden: {path}")
            continue
        try:
            blob.decode("utf-8")
        except UnicodeDecodeError:
            violations.append(f"non-UTF-8 staged file forbidden: {path}")
    if total > MAX_STAGED_TOTAL_BYTES:
        violations.append("candidate change exceeds total staged size limit")
    return GateResult(ok=not violations, violations=tuple(violations))


def run_gate(
    changed_files: list[str],
    diff_text: str,
    scope: ScopeSpec,
    *,
    worktree: Path | None = None,
    sensitive_literals: Iterable[str] = (),
) -> GateResult:
    scope_result = check_scope(changed_files, scope)
    sensitive_literals = tuple(sensitive_literals)
    secret_result = scan_secrets(diff_text, sensitive_literals=sensitive_literals)
    changed_path_text = "\n".join(changed_files)
    path_violations = tuple(
        ["injected credential material in staged path"]
        if any(
            fragment in changed_path_text
            for value in sensitive_literals
            if isinstance(value, str) and value
            for fragment in _secret_variants(value)
        )
        else []
    )
    path_result = GateResult(ok=not path_violations, violations=path_violations)
    tree_result = (
        inspect_staged_tree(worktree, changed_files)
        if worktree is not None
        else GateResult(ok=True)
    )
    return GateResult(
        ok=(scope_result.ok and secret_result.ok and path_result.ok and tree_result.ok),
        violations=(
            scope_result.violations
            + secret_result.violations
            + path_result.violations
            + tree_result.violations
        ),
    )
