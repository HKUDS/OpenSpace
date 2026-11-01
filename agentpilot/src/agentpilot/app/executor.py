"""Small local execution boundary; no remote scheduler semantics."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Callable, Generic, Iterable, Iterator, TypeVar

T = TypeVar("T")
R = TypeVar("R")


@dataclass(frozen=True)
class CompletedWork(Generic[T, R]):
    item: T
    result: R | None = None
    error: Exception | None = None


class LocalExecutor:
    """Run local independent slots and yield completion order."""

    def __init__(self, max_workers: int):
        if isinstance(max_workers, bool) or max_workers < 1:
            raise ValueError("max_workers must be positive")
        self.max_workers = max_workers

    def run(
        self,
        items: Iterable[T],
        function: Callable[[T], R],
        *,
        before_execute: Callable[[T], None] | None = None,
    ) -> Iterator[CompletedWork[T, R]]:
        values = tuple(items)

        def invoke(item: T) -> R:
            if before_execute is not None:
                before_execute(item)
            return function(item)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = {pool.submit(invoke, item): item for item in values}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    yield CompletedWork(item=item, result=future.result())
                except Exception as exc:
                    yield CompletedWork(item=item, error=exc)


__all__ = ["CompletedWork", "LocalExecutor"]
