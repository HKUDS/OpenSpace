"""Shared deterministic helpers for Harbor-style task records."""

from __future__ import annotations

import random

from rsi_arena.contracts import TaskRef, canonical_digest


def restrict_tasks(
    tasks: list[dict],
    *,
    levels: tuple[str, ...] = (),
    include_task_ids: tuple[str, ...] = (),
    exclude_task_ids: tuple[str, ...] = (),
    max_tasks: int = 0,
    seed: int = 0,
) -> list[dict]:
    """Return a deterministic category-stratified restricted subset."""

    excluded = set(exclude_task_ids)
    included = set(include_task_ids)
    picked = [
        task
        for task in tasks
        if task["task_id"] not in excluded
        and (not included or task["task_id"] in included)
        and (not levels or task["level"] in levels)
    ]
    picked.sort(key=lambda task: task["task_id"])
    if max_tasks and len(picked) > max_tasks:
        rng = random.Random(seed)
        by_category: dict[str, list[dict]] = {}
        for task in picked:
            by_category.setdefault(task["category"], []).append(task)
        categories = sorted(by_category)
        rng.shuffle(categories)
        result: list[dict] = []
        while len(result) < max_tasks and categories:
            for category in list(categories):
                group = by_category[category]
                if not group:
                    categories.remove(category)
                    continue
                result.append(group.pop(rng.randrange(len(group))))
                if len(result) >= max_tasks:
                    break
        picked = sorted(result, key=lambda task: task["task_id"])
    return picked


def to_task_refs(
    tasks: list[dict], *, benchmark_id: str = "tb2"
) -> list[TaskRef]:
    """Project task records into benchmark-qualified RSI task identities."""

    return [
        TaskRef(
            task_id=task["task_id"],
            cluster=f"{task['category']}/{task['level']}",
            repeatable=True,
            benchmark_id=benchmark_id,
            meta={
                "level": task["level"],
                "category": task["category"],
                "tags": task["tags"],
                "agent_timeout_sec": task["agent_timeout_sec"],
                "verifier_timeout_sec": task["verifier_timeout_sec"],
                "build_timeout_sec": task["build_timeout_sec"],
                "allow_internet": task["allow_internet"],
            },
        )
        for task in tasks
    ]


def task_list_digest(tasks: list[dict]) -> str:
    """Hash task membership independently of caller ordering."""

    return canonical_digest(sorted(task["task_id"] for task in tasks))


__all__ = ["restrict_tasks", "task_list_digest", "to_task_refs"]
