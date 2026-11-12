"""Base de données historique (SQLite) : moyennes par période, journal, audit.

Complète, sans la remplacer, la mémoire (1 h à pleine cadence) et les CSV (trace brute) :

* ``samples``  : une moyenne par instrument, par mesure et par période (60 s par défaut) ;
* ``journal``  : les messages du journal de l'interface, qui survivent ainsi aux redémarrages ;
* ``audit``    : qui a envoyé quelle commande, depuis quelle adresse, avec quel résultat.

Format « long » (une ligne par mesure) plutôt qu'une colonne par grandeur : les instruments ont
des mesures différentes, et ajouter un instrument ne change jamais le schéma.

Toutes les écritures passent par UN thread dédié : les workers d'acquisition n'attendent jamais
le disque. Si une écriture échoue, les moyennes sont conservées et retentées au cycle suivant.
"""
from __future__ import annotations

import json
import logging
import math
import queue
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

Extractor = Callable[[dict], dict[str, float | None]]

_PURGE_PERIOD_S = 3600.0
_MAX_PENDING_ROWS = 50_000  # plafond mémoire si le disque reste indisponible


def now_ms() -> int:
    return int(time.time() * 1000)


class HistoryDB:
    def __init__(
        self,
        path: str | Path,
        *,
        extractors: dict[str, Extractor],
        period_s: float = 60.0,
        retention_days: int = 90,
    ) -> None:
        self.path = Path(path)
        self.period_s = period_s
        self.retention_days = retention_days
        self._extractors = extractors

        self._acc: dict[str, dict[str, list[float]]] = {}  # instrument -> mesure -> [somme, n]
        self._acc_lock = threading.Lock()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._write_lock = threading.Lock()
        self._pending: list[tuple] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_flush = time.monotonic()
        self._last_purge = 0.0

        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ SQLite

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            with conn:  # commit / rollback automatique
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as c:
            c.execute("PRAGMA journal_mode=WAL")  # lectures de l'interface pendant les écritures
            c.execute(
                """CREATE TABLE IF NOT EXISTS samples (
                       inst_id TEXT NOT NULL, key TEXT NOT NULL, ts INTEGER NOT NULL, value REAL NOT NULL,
                       PRIMARY KEY (inst_id, key, ts)) WITHOUT ROWID"""
            )
            c.execute("CREATE TABLE IF NOT EXISTS journal (ts INTEGER NOT NULL, level TEXT NOT NULL, message TEXT NOT NULL)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_journal_ts ON journal(ts)")
            c.execute(
                """CREATE TABLE IF NOT EXISTS audit (
                       ts INTEGER NOT NULL, remote TEXT, inst_id TEXT NOT NULL, action TEXT NOT NULL,
                       params TEXT NOT NULL, outcome TEXT NOT NULL)"""
            )
            c.execute("CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit(ts)")

    # ------------------------------------------------------------------ cycle de vie

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="history-writer", daemon=True)
            self._thread.start()

    def close(self) -> None:
        """Arrête le thread d'écriture et enregistre ce qui est en cours (dernière période partielle)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(5)
            self._thread = None
        self.flush()

    def _run(self) -> None:
        while not self._stop.wait(0.5):
            try:
                self._cycle()
            except Exception:  # noqa: BLE001 - le thread d'écriture ne doit jamais mourir
                logger.exception("Erreur dans le thread d'écriture de l'historique")

    def flush(self) -> None:
        """Écrit immédiatement les moyennes en cours et les événements en attente."""
        self._cycle(force=True)

    def _cycle(self, force: bool = False) -> None:
        with self._write_lock:
            self._drain_events()
            now = time.monotonic()
            if force or now - self._last_flush >= self.period_s:
                self._flush_averages()
                self._last_flush = now
            if now - self._last_purge >= _PURGE_PERIOD_S:
                self._purge()
                self._last_purge = now

    # ------------------------------------------------------------------ entrées (appelées par les workers / Flask)

    def on_data(self, inst_id: str, data: dict) -> None:
        """Abonné du bus (sujet DATA) : cumule, ne touche jamais au disque."""
        extract = self._extractors.get(inst_id)
        if extract is None:
            return
        try:
            values = extract(data)
        except Exception:  # noqa: BLE001
            logger.exception("Extraction des valeurs à historiser impossible pour '%s'", inst_id)
            return
        with self._acc_lock:
            acc = self._acc.setdefault(inst_id, {})
            for key, value in values.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    continue
                slot = acc.setdefault(key, [0.0, 0])
                slot[0] += value
                slot[1] += 1

    def on_log(self, level: str, message: str, ts: float) -> None:
        """Abonné du bus (sujet LOG)."""
        self._queue.put(("journal", int(ts * 1000), level, message))

    def record_audit(self, remote: str | None, inst_id: str, action: str, params: dict, outcome: str) -> None:
        self._queue.put(("audit", now_ms(), remote, inst_id, action, json.dumps(params, ensure_ascii=False), outcome))

    # ------------------------------------------------------------------ écritures (thread d'écriture)

    def _drain_events(self) -> None:
        journal, audit = [], []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            (journal if item[0] == "journal" else audit).append(item[1:])
        try:
            with self._connect() as c:
                if journal:
                    c.executemany("INSERT INTO journal (ts, level, message) VALUES (?, ?, ?)", journal)
                if audit:
                    c.executemany(
                        "INSERT INTO audit (ts, remote, inst_id, action, params, outcome) VALUES (?, ?, ?, ?, ?, ?)", audit
                    )
        except sqlite3.Error:
            logger.exception("Écriture du journal / de l'audit impossible")

    def _flush_averages(self) -> None:
        with self._acc_lock:
            acc, self._acc = self._acc, {}
        ts = now_ms()
        rows = [
            (inst_id, key, ts, total / n)
            for inst_id, keys in acc.items()
            for key, (total, n) in keys.items()
            if n
        ]
        rows = self._pending + rows
        if not rows:
            return
        try:
            self.insert_samples(rows)
            self._pending = []
        except sqlite3.Error:
            logger.exception("Écriture des moyennes impossible : %d ligne(s) conservée(s) pour un nouvel essai", len(rows))
            self._pending = rows[-_MAX_PENDING_ROWS:]

    def insert_samples(self, rows: list[tuple]) -> None:
        """rows : (inst_id, key, ts_ms, value)."""
        with self._connect() as c:
            c.executemany("INSERT OR REPLACE INTO samples (inst_id, key, ts, value) VALUES (?, ?, ?, ?)", rows)

    def _purge(self) -> None:
        limit = now_ms() - self.retention_days * 86_400_000
        try:
            with self._connect() as c:
                c.execute("DELETE FROM samples WHERE ts < ?", (limit,))
                c.execute("DELETE FROM journal WHERE ts < ?", (limit,))  # l'audit est conservé (petit et précieux)
        except sqlite3.Error:
            logger.exception("Purge de l'historique impossible")

    # ------------------------------------------------------------------ lectures

    def buckets(
        self, inst_id: str, keys: list[str], since_s: float, until_s: float, bucket_s: float
    ) -> list[tuple[float, dict[str, float]]]:
        """Moyennes par tranche de ``bucket_s`` secondes, calculées par SQLite.

        Retourne [(timestamp_s_centre_de_tranche, {mesure: valeur})] triés par date.
        """
        if not keys or until_s <= since_s:
            return []
        bucket_ms = max(1, int(bucket_s * 1000))
        marks = ",".join("?" * len(keys))
        sql = (
            f"SELECT (ts / ?) * ? AS b, key, AVG(value) FROM samples "
            f"WHERE inst_id = ? AND key IN ({marks}) AND ts >= ? AND ts < ? GROUP BY b, key ORDER BY b"
        )
        args = [bucket_ms, bucket_ms, inst_id, *keys, int(since_s * 1000), int(until_s * 1000)]
        rows: dict[int, dict[str, float]] = {}
        with self._connect() as c:
            for bucket, key, value in c.execute(sql, args):
                rows.setdefault(bucket, {})[key] = value
        return [((b + bucket_ms / 2) / 1000, values) for b, values in sorted(rows.items())]

    def export_rows(self, inst_id: str, since_s: float) -> list[tuple[float, dict[str, float]]]:
        """Toutes les moyennes enregistrées d'un instrument, une ligne par date : [(ts_s, {mesure: valeur})]."""
        rows: dict[int, dict[str, float]] = {}
        with self._connect() as c:
            cursor = c.execute(
                "SELECT ts, key, value FROM samples WHERE inst_id = ? AND ts >= ? ORDER BY ts", (inst_id, int(since_s * 1000))
            )
            for ts, key, value in cursor:
                rows.setdefault(ts, {})[key] = value
        return [(ts / 1000, values) for ts, values in sorted(rows.items())]

    def recent_journal(self, limit: int = 200) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute("SELECT ts, level, message FROM journal ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
        return [{"ts": ts / 1000, "level": level, "message": message} for ts, level, message in reversed(rows)]

    def recent_audit(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT ts, remote, inst_id, action, params, outcome FROM audit ORDER BY ts DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {"ts": ts / 1000, "remote": remote, "instrument": inst, "action": action, "params": json.loads(params), "outcome": outcome}
            for ts, remote, inst, action, params, outcome in rows
        ]


# ====================================================================== historique hybride (mémoire + base)


def decimate(points: list[dict], keys: list[str], target: int) -> list[dict]:
    """Réduit ``points`` à ~``target`` points en moyennant par paquets (valeurs absentes ignorées)."""
    n = len(points)
    if target < 1 or n <= target:
        return points
    step = math.ceil(n / target)
    out = []
    for i in range(0, n, step):
        chunk = points[i : i + step]
        point: dict[str, Any] = {"timestamp": chunk[len(chunk) // 2]["timestamp"]}
        for key in keys:
            values = [p[key] for p in chunk if isinstance(p.get(key), (int, float))]
            point[key] = sum(values) / len(values) if values else None
        out.append(point)
    return out


def insert_gaps(points: list[dict], keys: list[str], gap_s: float) -> list[dict]:
    """Insère un point vide après un trou de plus de ``gap_s`` : la courbe est coupée, pas reliée."""
    out: list[dict] = []
    previous = None
    for point in points:
        if previous is not None and point["timestamp"] - previous > gap_s:
            out.append({"timestamp": previous + 0.001, **dict.fromkeys(keys)})
        out.append(point)
        previous = point["timestamp"]
    return out


def build_history(
    mem: list[dict],
    db: HistoryDB | None,
    inst_id: str,
    keys: list[str],
    *,
    since: float,
    now: float,
    max_points: int,
) -> dict[str, Any]:
    """Historique sur [since, now] : la mémoire (pleine cadence) pour le récent, la base pour le reste.

    ``mem`` : mesures en mémoire (dicts avec ``timestamp``). Retourne {"points": [...], "bucket_s": ..., "from_db": bool}.
    """
    keys = [k for k in keys if k != "timestamp"]  # l'horodatage est toujours présent, ce n'est pas une mesure
    spacing = max(1.0, math.ceil((now - since) / max_points))
    mem = [p for p in mem if p["timestamp"] >= since]
    mem_start = mem[0]["timestamp"] if mem else now

    older: list[dict] = []
    bucket_s = spacing
    if db is not None and mem_start > since + 5:
        bucket_s = max(spacing, db.period_s)
        rows = db.buckets(inst_id, keys, since, mem_start, bucket_s)
        older = insert_gaps(
            [{"timestamp": ts, **{k: values.get(k) for k in keys}} for ts, values in rows], keys, 3 * bucket_s
        )

    recent = [{"timestamp": p["timestamp"], **{k: p.get(k) for k in keys}} for p in mem]
    if recent:
        target = math.ceil((now - mem_start) / spacing)
        recent = insert_gaps(decimate(recent, keys, max(target, 1)), keys, 3 * max(spacing, 2.0))
        if older and recent[0]["timestamp"] - older[-1]["timestamp"] > 3 * bucket_s:
            older.append({"timestamp": older[-1]["timestamp"] + 0.001, **dict.fromkeys(keys)})

    points = [
        {"timestamp": p["timestamp"], **{k: (None if p[k] is None else round(p[k], 6)) for k in keys}} for p in older + recent
    ]
    return {"points": points, "bucket_s": bucket_s, "from_db": bool(older)}
