"""Mémoire des dernières mesures, de l'historique récent, des statuts et du journal."""
from __future__ import annotations

import threading
from collections import defaultdict, deque
from typing import Any

from fprv0.core.bus import DATA, LOG, STATUS, EventBus


class DataStore:
    def __init__(self, bus: EventBus, history_points: int = 3600, journal_size: int = 200) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, dict] = {}
        self._status: dict[str, dict] = {}
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=history_points))
        self._journal: deque = deque(maxlen=journal_size)
        bus.subscribe(DATA, self._on_data)
        bus.subscribe(STATUS, self._on_status)
        bus.subscribe(LOG, self._on_log)

    def _on_data(self, inst_id: str, data: dict) -> None:
        with self._lock:
            self._latest[inst_id] = data
            self._history[inst_id].append(data)

    def _on_status(self, inst_id: str, status: dict) -> None:
        with self._lock:
            self._status[inst_id] = status

    def _on_log(self, level: str, message: str, ts: float) -> None:
        with self._lock:
            self._journal.append({"ts": ts, "level": level, "message": message})

    def preload_journal(self, entries: list[dict]) -> None:
        """Reprend le journal persisté (base de données) au démarrage, avant les nouveaux messages."""
        with self._lock:
            old = list(self._journal)
            self._journal.clear()
            self._journal.extend(entries)
            self._journal.extend(old)

    def latest(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._latest)

    def status(self) -> dict[str, dict]:
        with self._lock:
            return dict(self._status)

    def journal(self) -> list[dict]:
        with self._lock:
            return list(self._journal)

    def history(
        self,
        inst_id: str,
        *,
        since: float | None = None,
        limit: int | None = None,
        keys: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            points = list(self._history.get(inst_id, ()))
        if since is not None:
            points = [p for p in points if p.get("timestamp", 0) > since]
        if limit is not None and limit >= 0:
            points = points[-limit:] if limit else []
        if keys:
            points = [{k: p.get(k) for k in keys} for p in points]
        return points
