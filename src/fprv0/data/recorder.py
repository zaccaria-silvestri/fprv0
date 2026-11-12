"""Enregistrement CSV fiable.

* un fichier par instrument et par jour : ``<data_dir>/<instrument>_AAAA-MM-JJ.csv`` (le jour est
  celui de l'heure locale de la machine ; rotation automatique à minuit) ;
* colonnes aplaties : ``vannes: [0, 1]`` devient ``vannes_1, vannes_2`` ;
* ``timestamp`` en ISO 8601 avec décalage horaire + ``epoch_s`` (secondes Unix) ;
* reprise après redémarrage : le fichier du jour est complété (ajout), en gardant son en-tête ;
* jamais de plantage de l'acquisition : une erreur disque est journalisée et on réessaie à la
  mesure suivante ; ``close()`` doit être appelé à l'arrêt.
"""
from __future__ import annotations

import csv
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

logger = logging.getLogger(__name__)

_ERROR_LOG_PERIOD_S = 60.0


def flatten(data: dict[str, Any], prefix: str = "") -> dict[str, Any]:
    """Aplatit listes et sous-dictionnaires : ``{"v": [0, 1]}`` -> ``{"v_1": 0, "v_2": 1}``."""
    flat: dict[str, Any] = {}
    for key, value in data.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten(value, prefix=f"{name}_"))
        elif isinstance(value, (list, tuple)):
            flat.update(flatten({str(i + 1): item for i, item in enumerate(value)}, prefix=f"{name}_"))
        else:
            flat[name] = "" if value is None else value
    return flat


class _Sink:
    def __init__(self, path: Path, day: str, handle: TextIO, fieldnames: list[str]) -> None:
        self.path = path
        self.day = day
        self.handle = handle
        self.fieldnames = fieldnames
        self.writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore", restval="", lineterminator="\n")
        self.warned_extra = False

    def close(self) -> None:
        try:
            self.handle.close()
        except OSError:
            pass


class CSVRecorder:
    def __init__(self, data_dir: str | Path = "data") -> None:
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._sinks: dict[str, _Sink] = {}
        self._lock = threading.Lock()
        self._last_error_log = 0.0

    # Callback du bus : on_data(inst_id=..., data=...)
    def on_data(self, inst_id: str, data: dict) -> None:
        try:
            self._write(inst_id, data)
        except OSError as exc:
            self._drop_sink(inst_id)
            now = time.monotonic()
            if now - self._last_error_log > _ERROR_LOG_PERIOD_S:
                self._last_error_log = now
                logger.error("Écriture CSV impossible pour '%s' : %s (nouvel essai à la prochaine mesure)", inst_id, exc)

    def _write(self, inst_id: str, data: dict) -> None:
        ts = float(data.get("timestamp") or time.time())
        moment = datetime.fromtimestamp(ts).astimezone()
        row = {
            "timestamp": moment.isoformat(timespec="milliseconds"),
            "epoch_s": round(ts, 3),
            **flatten({k: v for k, v in data.items() if k != "timestamp"}),
        }
        day = moment.strftime("%Y-%m-%d")

        with self._lock:
            sink = self._sinks.get(inst_id)
            if sink is None or sink.day != day:
                if sink is not None:
                    sink.close()
                sink = self._open_sink(inst_id, day, row)
                self._sinks[inst_id] = sink

            extra = set(row) - set(sink.fieldnames)
            if extra and not sink.warned_extra:
                sink.warned_extra = True
                logger.warning(
                    "'%s' : colonnes absentes de l'en-tête de %s, ignorées : %s", inst_id, sink.path.name, sorted(extra)
                )
            sink.writer.writerow(row)
            sink.handle.flush()

    def _open_sink(self, inst_id: str, day: str, first_row: dict) -> _Sink:
        path = self.data_dir / f"{inst_id}_{day}.csv"
        existing_header = self._read_header(path)
        handle = open(path, "a", newline="", encoding="utf-8")
        try:
            sink = _Sink(path, day, handle, existing_header or list(first_row))
            if not existing_header:
                sink.writer.writeheader()
        except Exception:
            handle.close()
            raise
        return sink

    @staticmethod
    def _read_header(path: Path) -> list[str] | None:
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                return next(csv.reader(fh), None) or None
        except FileNotFoundError:
            return None

    def _drop_sink(self, inst_id: str) -> None:
        with self._lock:
            sink = self._sinks.pop(inst_id, None)
        if sink is not None:
            sink.close()

    def close(self) -> None:
        with self._lock:
            sinks, self._sinks = list(self._sinks.values()), {}
        for sink in sinks:
            sink.close()
