"""Persisted one-shot schedules (currently: resume an Immich queue at a given time)."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class Schedule:
    key: str          # unique per target, e.g. "immich_resume:videoConversion"
    verb: str
    args: dict
    run_at: float     # unix seconds
    attempts: int = 0


class ScheduleStore:
    MAX_ATTEMPTS = 10

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._items: dict[str, Schedule] = {}
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            for item in data:
                s = Schedule(**item)
                self._items[s.key] = s

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(s) for s in self._items.values()], indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def put(self, schedule: Schedule) -> None:
        """Add or replace the schedule with the same key."""
        with self._lock:
            self._items[schedule.key] = schedule
            self._save()

    def cancel(self, key: str) -> bool:
        with self._lock:
            removed = self._items.pop(key, None) is not None
            if removed:
                self._save()
            return removed

    def list(self) -> list[Schedule]:
        with self._lock:
            return sorted(self._items.values(), key=lambda s: s.run_at)

    def due(self, now: float) -> list[Schedule]:
        with self._lock:
            return [s for s in self._items.values() if s.run_at <= now]

    def record_failure(self, key: str) -> bool:
        """Count a failed attempt. Returns False if the schedule was dropped."""
        with self._lock:
            s = self._items.get(key)
            if s is None:
                return False
            s.attempts += 1
            if s.attempts >= self.MAX_ATTEMPTS:
                del self._items[key]
                self._save()
                return False
            self._save()
            return True
