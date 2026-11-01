from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .models import ScheduleDefinition


SCHEDULE_FILE_NAME = "scheduled_tasks.json"


def get_default_schedule_path(workspace_dir: str | os.PathLike[str]) -> Path:
    return Path(workspace_dir).expanduser().resolve() / ".openspace" / SCHEDULE_FILE_NAME


def has_enabled_schedules(workspace_dir: str | os.PathLike[str]) -> bool:
    """Return true when the workspace has schedules that should wake the loop."""
    return any(
        schedule.enabled and not schedule.paused and schedule.next_run_at
        for schedule in ScheduleStore.for_workspace(workspace_dir).load()
    )


class ScheduleStore:
    """JSON-backed schedule store.

    OpenSpace writes ``.claude/scheduled_tasks.json`` with a compact cron-task shape.
    OpenSpace stores the richer long-horizon schedule contract in
    ``.openspace/scheduled_tasks.json`` so owner/session/policy/audit data are
    durable from the start.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    @classmethod
    def for_workspace(cls, workspace_dir: str | os.PathLike[str]) -> "ScheduleStore":
        return cls(get_default_schedule_path(workspace_dir))

    def load(self) -> list[ScheduleDefinition]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return []
        except OSError:
            return []
        try:
            data = json.loads(raw or "{}")
        except json.JSONDecodeError:
            return []
        items = data.get("schedules") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return []
        schedules: list[ScheduleDefinition] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                schedules.append(ScheduleDefinition.from_dict(item))
            except Exception:
                continue
        return schedules

    def save(self, schedules: list[ScheduleDefinition]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "schedules": [schedule.to_dict() for schedule in schedules],
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def list(self) -> list[ScheduleDefinition]:
        return self.load()

    def get(self, schedule_id: str) -> ScheduleDefinition | None:
        for schedule in self.load():
            if schedule.id == schedule_id:
                return schedule
        return None

    def upsert(self, schedule: ScheduleDefinition) -> None:
        schedules = self.load()
        for idx, current in enumerate(schedules):
            if current.id == schedule.id:
                schedules[idx] = schedule
                self.save(schedules)
                return
        schedules.append(schedule)
        self.save(schedules)

    def delete(self, schedule_id: str) -> bool:
        schedules = self.load()
        remaining = [schedule for schedule in schedules if schedule.id != schedule_id]
        if len(remaining) == len(schedules):
            return False
        self.save(remaining)
        return True

    def replace_all(self, schedules: list[ScheduleDefinition]) -> None:
        self.save(schedules)

    def raw(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "schedules": [schedule.visible_dict() for schedule in self.load()],
        }


__all__ = [
    "SCHEDULE_FILE_NAME",
    "ScheduleStore",
    "get_default_schedule_path",
    "has_enabled_schedules",
]
