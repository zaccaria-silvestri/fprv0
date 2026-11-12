"""Worker d'instrument : un thread par instrument, résilient aux pannes.

États (``State``) :

    CONNECTING ──ok──▶ CONNECTED ◀──succès── DEGRADED
        │                  │ échec de mesure     ▲
        │ échec            └─────────────────────┘
        ▼                  │ max_failures échecs consécutifs
    DISCONNECTED ◀─────────┘
        │ attente (backoff exponentiel + gigue)
        └──▶ CONNECTING …            STOPPED = arrêt demandé

Garanties :
* mesures ET commandes s'exécutent dans CE thread, jamais dans celui de l'interface : une
  vanne qui ne répond pas ne gèle rien et le client HTTP n'est jamais utilisé en concurrence ;
* toutes les attentes sont interruptibles : ``request_stop()`` est pris en compte immédiatement ;
* la cadence d'acquisition ne dérive pas (échéances absolues, pas de rafale de rattrapage) ;
* une commande n'est acceptée que si l'instrument est joignable : elle n'est jamais mise en
  attente pour être exécutée « plus tard », à une reconnexion inopinée (sûreté des vannes).
"""
from __future__ import annotations

import queue
import random
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass
from enum import Enum
from typing import Any

from fprv0.core.bus import DATA, STATUS, EventBus
from fprv0.core.commands import CommandError


class State(str, Enum):
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DEGRADED = "degraded"
    DISCONNECTED = "disconnected"
    STOPPED = "stopped"


_ACTIVE = (State.CONNECTED, State.DEGRADED)


class NotConnectedError(RuntimeError):
    """L'instrument n'est pas joignable : la commande est refusée."""


@dataclass
class _Job:
    name: str
    kwargs: dict[str, Any]
    future: Future


def _short(exc: BaseException, limit: int = 200) -> str:
    text = f"{type(exc).__name__}: {exc}".replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


class InstrumentWorker(threading.Thread):
    def __init__(
        self,
        instrument_id: str,
        driver,
        bus: EventBus,
        *,
        poll_interval: float = 1.0,
        retry_min: float = 1.0,
        retry_max: float = 30.0,
        max_failures: int = 3,
    ) -> None:
        super().__init__(name=f"worker-{instrument_id}", daemon=True)
        self.instrument_id = instrument_id
        self.driver = driver
        self.bus = bus
        self.poll_interval = poll_interval
        self.retry_min = retry_min
        self.retry_max = retry_max
        self.max_failures = max_failures

        self._stop_event = threading.Event()
        self._jobs: queue.Queue[_Job | None] = queue.Queue()
        self._lock = threading.Lock()
        self._state = State.STOPPED
        self._detail = ""
        self._since = time.time()
        self._last_ok: float | None = None
        self._failures = 0
        self._reconnects = 0
        self._ever_connected = False
        self._opened = False

    # ------------------------------------------------------------------ API publique

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked()

    def submit_command(self, name: str, kwargs: dict[str, Any] | None = None) -> Future:
        """Valide puis met la commande en file. Lève CommandError ou NotConnectedError tout de suite."""
        spec = self.driver.commands().get(name)
        if spec is None:
            raise CommandError(f"Commande inconnue : '{name}'")
        clean = spec.validate(kwargs or {})
        job = _Job(name, clean, Future())
        with self._lock:
            if self._state not in _ACTIVE:
                raise NotConnectedError(f"'{self.instrument_id}' n'est pas connecté (état : {self._state.value})")
            self._jobs.put(job)
        return job.future

    def request_stop(self) -> None:
        self._stop_event.set()
        self._jobs.put(None)  # réveille l'attente sur la file

    # ------------------------------------------------------------------ boucle principale

    def run(self) -> None:
        attempt = 0
        try:
            while not self._stop_event.is_set():
                self._set_state(State.CONNECTING, "connexion en cours")
                self._opened = True
                try:
                    self.driver.connect()
                except Exception as exc:  # noqa: BLE001
                    attempt += 1
                    delay = self._backoff(attempt)
                    self._safe_disconnect()
                    self._set_state(
                        State.DISCONNECTED, f"connexion impossible ({_short(exc)}) — nouvel essai dans {delay:.0f} s", "WARNING"
                    )
                    if self._stop_event.wait(delay):
                        break
                    continue

                attempt = 0
                self._failures = 0
                if self._ever_connected:
                    self._reconnects += 1
                self._ever_connected = True
                self._set_state(State.CONNECTED, "")

                reason = self._poll_loop()
                self._safe_disconnect()
                if self._stop_event.is_set():
                    break
                delay = self._backoff(1)
                self._set_state(State.DISCONNECTED, f"{reason} — reconnexion dans {delay:.0f} s", "WARNING")
                if self._stop_event.wait(delay):
                    break
        finally:
            self._safe_disconnect()
            self._set_state(State.STOPPED, "")

    def _poll_loop(self) -> str:
        """Boucle d'acquisition. Retourne la raison de sortie (perte de l'instrument ou arrêt)."""
        next_poll = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            if now >= next_poll:
                error = self._poll_once()
                if error is not None and self._failures >= self.max_failures:
                    return f"{self._failures} échecs consécutifs ({error})"
                next_poll += self.poll_interval
                now = time.monotonic()
                if next_poll < now:  # en retard : on repart de maintenant, sans rafale de rattrapage
                    next_poll = now
            try:
                job = self._jobs.get(timeout=max(0.0, next_poll - now))
            except queue.Empty:
                continue
            if job is None:
                continue  # simple réveil (arrêt)
            self._run_job(job)
            next_poll = time.monotonic()  # relit l'état de l'instrument juste après une commande
        return "arrêt demandé"

    def _poll_once(self) -> str | None:
        try:
            data = self.driver.poll()
        except Exception as exc:  # noqa: BLE001
            self._failures += 1
            error = _short(exc)
            self._set_state(State.DEGRADED, f"échec de mesure {self._failures}/{self.max_failures} : {error}", "WARNING")
            return error

        self._failures = 0
        with self._lock:
            self._last_ok = time.time()
            recovered = self._state is State.DEGRADED
        if recovered:
            self._set_state(State.CONNECTED, "rétabli")
        if data:
            self.bus.publish(DATA, inst_id=self.instrument_id, data=data)
        return None

    def _run_job(self, job: _Job) -> None:
        if not job.future.set_running_or_notify_cancel():
            return
        try:
            result = self.driver.execute(job.name, job.kwargs)
        except Exception as exc:  # noqa: BLE001
            job.future.set_exception(exc)
            self.bus.log("ERROR", f"[{self.instrument_id}] commande {job.name} {job.kwargs} échouée : {_short(exc)}")
        else:
            job.future.set_result(result)
            self.bus.log("INFO", f"[{self.instrument_id}] commande {job.name} {job.kwargs} exécutée")

    # ------------------------------------------------------------------ utilitaires

    def _backoff(self, attempt: int) -> float:
        delay = min(self.retry_max, self.retry_min * (2 ** max(0, attempt - 1)))
        return delay + random.uniform(0, 0.1 * delay)

    def _safe_disconnect(self) -> None:
        if not self._opened:
            return
        self._opened = False
        try:
            self.driver.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.bus.log("WARNING", f"[{self.instrument_id}] erreur à la déconnexion : {_short(exc)}")

    def _snapshot_locked(self) -> dict[str, Any]:
        return {
            "state": self._state.value,
            "detail": self._detail,
            "since": self._since,
            "last_ok": self._last_ok,
            "failures": self._failures,
            "reconnects": self._reconnects,
        }

    def _set_state(self, state: State, detail: str = "", level: str = "INFO") -> None:
        with self._lock:
            if state == self._state and detail == self._detail:
                return
            if state != self._state:
                self._since = time.time()
            self._state, self._detail = state, detail
            snapshot = self._snapshot_locked()
        if state not in _ACTIVE:
            self._reject_jobs(f"'{self.instrument_id}' n'est plus connecté ({state.value})")
        self.bus.publish(STATUS, inst_id=self.instrument_id, status=snapshot)
        self.bus.log(level, f"[{self.instrument_id}] {state.value}" + (f" — {detail}" if detail else ""))

    def _reject_jobs(self, reason: str) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            if job is not None and job.future.set_running_or_notify_cancel():
                job.future.set_exception(NotConnectedError(reason))
