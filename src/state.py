"""Persistent target state management."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class StateUpdateResult:
    """Outcome of writing a target state update."""

    previous_status: str | None
    previous_signature: str | None
    previous_detail_recovered: bool
    current_status: str
    warning_threshold_crossed: bool
    state: dict[str, Any]


class StateStore:
    """Thread-safe JSON-backed state store."""

    def __init__(self, path: Path) -> None:
        """Initialize the state store."""

        self.path = path
        self._lock = asyncio.Lock()

    async def load(self) -> dict[str, dict[str, Any]]:
        """Load state from disk, creating an empty file if missing."""

        async with self._lock:
            return self._read_unlocked()

    async def update_target_state(
        self,
        label: str,
        status: str,
        signature: str | None = None,
        detail_recovered: bool | None = None,
        last_alert_sent: str | None = None,
    ) -> StateUpdateResult:
        """Update a target state record and persist it."""

        async with self._lock:
            data = self._read_unlocked()
            previous = data.get(label, {})
            previous_unknowns = int(previous.get("consecutive_unknowns", 0))
            consecutive_unknowns = previous_unknowns + 1 if status == "unknown" else 0
            record = {
                "status": status,
                "last_signature": signature or previous.get("last_signature"),
                "detail_recovered": detail_recovered if detail_recovered is not None else bool(previous.get("detail_recovered", False)),
                "last_check": datetime.now(timezone.utc).isoformat(),
                "last_alert_sent": last_alert_sent or previous.get("last_alert_sent"),
                "consecutive_unknowns": consecutive_unknowns,
                "check_count": int(previous.get("check_count", 0)) + 1,
            }
            data[label] = record
            self._write_unlocked(data)
            return StateUpdateResult(
                previous_status=previous.get("status"),
                previous_signature=previous.get("last_signature"),
                previous_detail_recovered=bool(previous.get("detail_recovered", False)),
                current_status=status,
                warning_threshold_crossed=previous_unknowns <= 5 < consecutive_unknowns,
                state=record,
            )

    async def mark_alert_sent(self, label: str) -> dict[str, Any]:
        """Update only the last alert timestamp for a target."""

        async with self._lock:
            data = self._read_unlocked()
            record = data.setdefault(
                label,
                {
                    "status": "unknown",
                    "last_signature": None,
                    "detail_recovered": False,
                    "last_check": None,
                    "last_alert_sent": None,
                    "consecutive_unknowns": 0,
                    "check_count": 0,
                },
            )
            record["last_alert_sent"] = datetime.now(timezone.utc).isoformat()
            self._write_unlocked(data)
            return record

    def _read_unlocked(self) -> dict[str, dict[str, Any]]:
        """Read state from disk without taking the lock."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write_unlocked({})
            return {}
        raw = self.path.read_text(encoding="utf-8").strip()
        if not raw:
            self._write_unlocked({})
            return {}
        return json.loads(raw)

    def _write_unlocked(self, data: dict[str, dict[str, Any]]) -> None:
        """Write state to disk atomically without taking the lock."""

        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        temp_path.replace(self.path)
