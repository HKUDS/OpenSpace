"""Task samples for Terminal-Bench 2.1.

The default sample is intentionally fixed so model runs are comparable over
time. It covers the largest task families, includes easy/medium/hard tasks, and
keeps a few long-timeout tasks to expose slow-tool and long-horizon behavior.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SampleTask:
    slug: str
    category: str
    difficulty: str
    reason: str

    @property
    def harbor_name(self) -> str:
        return f"terminal-bench/{self.slug}"


SMOKE_TASKS: tuple[SampleTask, ...] = (
    SampleTask(
        "fix-git",
        "software-engineering",
        "easy",
        "Fast harness sanity check with a small git workflow.",
    ),
)


SAMPLE_20_TASKS: tuple[SampleTask, ...] = (
    SampleTask(
        "fix-git",
        "software-engineering",
        "easy",
        "Fast end-to-end smoke signal for git/file edits.",
    ),
    SampleTask(
        "build-cython-ext",
        "debugging",
        "medium",
        "Python packaging plus native-extension debugging.",
    ),
    SampleTask(
        "configure-git-webserver",
        "system-administration",
        "hard",
        "System setup and git service configuration.",
    ),
    SampleTask(
        "raman-fitting",
        "scientific-computing",
        "medium",
        "Numerical fitting and JSON output generation.",
    ),
    SampleTask(
        "break-filter-js-from-html",
        "security",
        "medium",
        "Adversarial security task with a compact verifier.",
    ),
    SampleTask(
        "query-optimize",
        "data-science",
        "medium",
        "SQL/query reasoning and performance improvement.",
    ),
    SampleTask(
        "multi-source-data-merger",
        "data-processing",
        "medium",
        "Data cleaning and multi-input integration.",
    ),
    SampleTask(
        "extract-elf",
        "file-operations",
        "medium",
        "Binary/file inspection without large runtime cost.",
    ),
    SampleTask(
        "pytorch-model-cli",
        "model-training",
        "medium",
        "ML code path with CLI and model artifact expectations.",
    ),
    SampleTask(
        "caffe-cifar-10",
        "machine-learning",
        "medium",
        "Classic ML dependency/setup workload with longer timeout.",
    ),
    SampleTask(
        "adaptive-rejection-sampler",
        "scientific-computing",
        "medium",
        "Statistical programming task distinct from curve fitting.",
    ),
    SampleTask(
        "password-recovery",
        "security",
        "hard",
        "Hard security task that tests search/tool persistence.",
    ),
    SampleTask(
        "cancel-async-tasks",
        "software-engineering",
        "hard",
        "Concurrency bug fixing in application code.",
    ),
    SampleTask(
        "kv-store-grpc",
        "software-engineering",
        "medium",
        "Service implementation with protocol-level tests.",
    ),
    SampleTask(
        "mteb-retrieve",
        "data-science",
        "medium",
        "Retrieval/data-science task with moderate runtime.",
    ),
    SampleTask(
        "sqlite-db-truncate",
        "debugging",
        "medium",
        "Database recovery/debugging style task.",
    ),
    SampleTask(
        "qemu-startup",
        "system-administration",
        "medium",
        "VM/system workflow that catches environment handling issues.",
    ),
    SampleTask(
        "portfolio-optimization",
        "optimization",
        "medium",
        "Optimization family coverage with a longer-running verifier.",
    ),
    SampleTask(
        "regex-chess",
        "software-engineering",
        "hard",
        "Hard symbolic/programming task with unusual constraints.",
    ),
    SampleTask(
        "video-processing",
        "video-processing",
        "hard",
        "Singleton category and multimedia/long-runtime coverage.",
    ),
)


def distribution(tasks: tuple[SampleTask, ...]) -> dict[str, Counter[str]]:
    return {
        "category": Counter(task.category for task in tasks),
        "difficulty": Counter(task.difficulty for task in tasks),
    }


def normalize_task_name(task: str) -> str:
    task = task.strip()
    if not task:
        raise ValueError("Task name cannot be empty")
    if task.startswith("terminal-bench/"):
        return task
    return f"terminal-bench/{task}"


def sample_for_name(name: str) -> tuple[SampleTask, ...]:
    normalized = name.strip().lower()
    if normalized in {"smoke", "smoke1"}:
        return SMOKE_TASKS
    if normalized in {"sample20", "sample", "default"}:
        return SAMPLE_20_TASKS
    if normalized == "full":
        return ()
    raise ValueError(f"Unknown sample: {name}")

