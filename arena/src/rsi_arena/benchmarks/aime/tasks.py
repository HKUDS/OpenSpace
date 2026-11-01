"""AIME 2024+2025 task snapshot → Harbor task directories.

The committed snapshot (``data/aime_2024_2025.json``) is the source of truth:
60 problems, integer answers 0–999. Task *content* is pinned by the snapshot
digest — it enters
the bench identity, so a drifting snapshot invalidates caches loudly.

``build_task_dirs`` renders each problem into a Harbor task with a dedicated
verifier image.  The candidate still writes ``/app/answer.txt``; a trusted
agent bridge (outside this package) strictly extracts that file after the
candidate stops and publishes ``/logs/artifacts/rsi-arena-answer.txt``.  The
fresh verifier consumes only that bridge artifact.  It never shares the agent
filesystem and never treats a candidate-written reward as authoritative.

The answer exists only in ``tests/`` and ``solution/``.  The agent environment
is offline (``allow_internet = false``).  Generation is deterministic and
idempotent: a stamp file carries the complete content digest and the tree is
rebuilt only when it moves.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import stat
from pathlib import Path

from rsi_arena.contracts import canonical_digest

SNAPSHOT_PATH = Path(__file__).resolve().parent / "data" / "aime_2024_2025.json"
_STAMP_NAME = ".snapshot_digest"

_DOCKERFILE = """\
FROM python:3.11-slim

# tmux + asciinema baked in: Terminus 2 needs them for its session, and the
# agent container is offline (allow_internet = false) so the agent's own
# fallback install cannot work (same package set as TB2 task images).
RUN apt-get update && apt-get install -y --no-install-recommends \\
    tmux asciinema procps \\
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir sympy==1.13.1 numpy==2.1.2

WORKDIR /app
"""

_TEST_DOCKERFILE = """\
FROM python:3.11-slim

RUN mkdir -p /tests /logs/verifier
COPY test.sh /tests/test.sh
RUN chmod 0555 /tests/test.sh
"""

_TASK_TOML = """\
schema_version = "1.1"
# /logs/artifacts is Harbor's conventional transfer directory.  The trusted
# agent bridge publishes rsi-arena-answer.txt there after the candidate exits.
artifacts = ["/logs/artifacts"]

[task]
name = "aime/{task_id}"
description = "AIME {year} {contest} Problem {number}: produce the integer answer."
keywords = ["competition-math", "aime"]

[metadata]
difficulty = "{difficulty}"
category = "competition-math"
tags = ["aime", "aime-{year}", "contest-{contest_lower}"]

[verifier]
timeout_sec = 120.0
environment_mode = "separate"

[agent]
timeout_sec = {agent_timeout_sec}

[environment]
build_timeout_sec = 600.0
cpus = 1
memory_mb = 2048
storage_mb = 4096
gpus = 0
allow_internet = false
mcp_servers = []

[verifier.env]

[environment.env]

[solution.env]
"""

_INSTRUCTION = """\
Solve the following competition math problem (AIME {year} {contest}, Problem {number}).

{problem}

The answer is an integer between 0 and 999. You may use any tools available in
this environment (for example Python with sympy/numpy) to reason and compute.

When you are done, write the final answer to /app/answer.txt — the integer
only, with no other text.
"""

_TEST_SH = """\
#!/bin/bash
set -euo pipefail

mkdir -p /logs/verifier
if python3 - "{answer}" <<'PY'
import re
import stat
import sys
from pathlib import Path

path = Path("/logs/artifacts/rsi-arena-answer.txt")
try:
    info = path.lstat()
    raw = path.read_bytes()
except OSError:
    raise SystemExit(1)

if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or len(raw) > 16:
    raise SystemExit(1)
try:
    text = raw.decode("ascii")
except UnicodeDecodeError:
    raise SystemExit(1)
if text.endswith("\\n"):
    text = text[:-1]
if re.fullmatch(r"(?:0|[1-9][0-9]{{0,2}})", text) is None:
    raise SystemExit(1)
value = int(text)
raise SystemExit(0 if 0 <= value <= 999 and value == int(sys.argv[1]) else 1)
PY
then
  echo 1 > /logs/verifier/reward.txt
else
  echo 0 > /logs/verifier/reward.txt
fi
"""

_SOLVE_SH = """\
#!/bin/bash
echo "{answer}" > /app/answer.txt
"""


def load_snapshot(snapshot_path: Path = SNAPSHOT_PATH) -> list[dict]:
    problems = json.loads(Path(snapshot_path).read_text())
    if not problems:
        raise ValueError(f"empty AIME snapshot at {snapshot_path}")
    return problems


def snapshot_digest(problems: list[dict]) -> str:
    return canonical_digest(problems)


def content_digest(problems: list[dict], *, agent_timeout_sec: float = 1200.0) -> str:
    """Pin over the snapshot AND the rendering templates.

    The rendered tree is problems × templates; a template change (image
    packages, verifier logic, instruction wording) changes what agents see
    and how they are graded just as much as a snapshot change, so it must
    move the stamp and the bench identity too.
    """
    return canonical_digest(
        {
            "problems": problems,
            "templates": [
                _DOCKERFILE,
                _TEST_DOCKERFILE,
                _TASK_TOML,
                _INSTRUCTION,
                _TEST_SH,
                _SOLVE_SH,
            ],
            "agent_timeout_sec": float(agent_timeout_sec),
        }
    )


def _rendered_tree_digest(tasks_root: Path) -> str:
    """Hash the actual generated tree, rejecting ambient filesystem tricks."""

    entries: list[dict] = []
    for path in sorted(
        Path(tasks_root).rglob("*"),
        key=lambda item: item.relative_to(tasks_root).as_posix(),
    ):
        relative = path.relative_to(tasks_root).as_posix()
        if relative == _STAMP_NAME:
            continue
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError(f"AIME rendered tree contains a symlink: {relative}")
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": relative, "kind": "directory"})
            continue
        if not stat.S_ISREG(info.st_mode):
            raise ValueError(f"AIME rendered tree contains a special file: {relative}")
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        entries.append(
            {
                "path": relative,
                "kind": "file",
                "sha256": digest.hexdigest(),
                "executable": bool(info.st_mode & 0o111),
            }
        )
    return canonical_digest(entries)


def difficulty_of(number: int) -> str:
    # AIME orders problems roughly by difficulty within a contest.
    if number <= 5:
        return "easy"
    if number <= 10:
        return "medium"
    return "hard"


def build_task_dirs(
    problems: list[dict],
    tasks_root: Path,
    *,
    agent_timeout_sec: float = 1200.0,
) -> None:
    """Render the snapshot into Harbor task directories (idempotent)."""
    tasks_root = Path(tasks_root)
    digest = content_digest(problems, agent_timeout_sec=agent_timeout_sec)
    stamp = tasks_root / _STAMP_NAME
    try:
        recorded = json.loads(stamp.read_text())
        if (
            isinstance(recorded, dict)
            and recorded.get("source_digest") == digest
            and recorded.get("rendered_tree_digest")
            == _rendered_tree_digest(tasks_root)
        ):
            return
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
        pass
    if tasks_root.exists():
        shutil.rmtree(tasks_root)
    tasks_root.mkdir(parents=True)

    for p in problems:
        task_dir = tasks_root / p["task_id"]
        (task_dir / "environment").mkdir(parents=True)
        (task_dir / "tests").mkdir()
        (task_dir / "solution").mkdir()

        (task_dir / "task.toml").write_text(
            _TASK_TOML.format(
                task_id=p["task_id"],
                year=p["year"],
                contest=p["contest"],
                contest_lower=p["contest"].lower(),
                number=p["number"],
                difficulty=difficulty_of(p["number"]),
                agent_timeout_sec=float(agent_timeout_sec),
            )
        )
        (task_dir / "environment" / "Dockerfile").write_text(_DOCKERFILE)
        (task_dir / "tests" / "Dockerfile").write_text(_TEST_DOCKERFILE)
        (task_dir / "instruction.md").write_text(
            _INSTRUCTION.format(
                year=p["year"],
                contest=p["contest"],
                number=p["number"],
                problem=p["problem"],
            )
        )
        test_sh = task_dir / "tests" / "test.sh"
        test_sh.write_text(_TEST_SH.format(answer=p["answer"]))
        test_sh.chmod(0o755)
        solve_sh = task_dir / "solution" / "solve.sh"
        solve_sh.write_text(_SOLVE_SH.format(answer=p["answer"]))
        solve_sh.chmod(0o755)

    stamp.write_text(
        json.dumps(
            {
                "source_digest": digest,
                "rendered_tree_digest": _rendered_tree_digest(tasks_root),
            },
            sort_keys=True,
        )
        + "\n"
    )
