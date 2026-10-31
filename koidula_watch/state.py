"""Состояние между проверками (переживает перезапуск контейнера) и журнал проверок."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class State:
    # час слота ("10:00") -> ISO-время последнего уведомления о нём
    notified_free: dict[str, str] = field(default_factory=dict)
    # слоты, свободные на прошлой проверке
    last_free: list[str] = field(default_factory=list)
    last_status: str = ""
    opened_full_notified: bool = False
    unclear_notified: bool = False
    consecutive_errors: int = 0
    error_alert_active: bool = False
    last_error_alert_at: str = ""
    last_heartbeat_date: str = ""
    checks_total: int = 0
    last_check_at: str = ""
    stopped: bool = False
    stop_reason: str = ""
    started_notified: bool = False


class StateStore:
    def __init__(self, path: Path):
        self.path = path

    def load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return State()
        known = State.__dataclass_fields__
        return State(**{k: v for k, v in raw.items() if k in known})

    def save(self, state: State) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(state), handle, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)


class CheckJournal:
    """Одна строка JSON на проверку: data/checks.jsonl."""

    def __init__(self, path: Path):
        self.path = path

    def write(self, record: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
