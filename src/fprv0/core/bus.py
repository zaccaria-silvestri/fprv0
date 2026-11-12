"""Bus d'événements minimal et thread-safe (sans dépendance à Qt).

Les abonnés sont appelés de façon synchrone dans le thread de l'émetteur (un worker
d'instrument) : un callback doit donc être court et ne jamais bloquer (typiquement : mettre
un message dans une file). Une exception dans un abonné est journalisée mais n'interrompt
ni l'acquisition ni les autres abonnés.

Sujets :
    DATA    inst_id: str, data: dict        nouvelle mesure
    STATUS  inst_id: str, status: dict      changement d'état d'un instrument
    LOG     level: str, message: str, ts: float
"""
from __future__ import annotations

import logging
import threading
import time
from collections import defaultdict
from collections.abc import Callable

DATA = "data"
STATUS = "status"
LOG = "log"

logger = logging.getLogger("fprv0.bus")
app_logger = logging.getLogger("fprv0")


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, list[Callable[..., None]]] = defaultdict(list)
        self._lock = threading.Lock()

    def subscribe(self, topic: str, callback: Callable[..., None]) -> Callable[[], None]:
        """Abonne ``callback(**payload)`` à ``topic``. Retourne une fonction de désabonnement."""
        with self._lock:
            self._subs[topic].append(callback)

        def unsubscribe() -> None:
            with self._lock:
                if callback in self._subs[topic]:
                    self._subs[topic].remove(callback)

        return unsubscribe

    def publish(self, topic: str, **payload) -> None:
        with self._lock:
            callbacks = list(self._subs.get(topic, ()))
        for callback in callbacks:
            try:
                callback(**payload)
            except Exception:
                logger.exception("Abonné %r en échec sur le sujet '%s'", callback, topic)

    def log(self, level: str, message: str) -> None:
        """Écrit dans le logging standard ET publie pour le journal de l'interface."""
        app_logger.log(getattr(logging, level.upper(), logging.INFO), message)
        self.publish(LOG, level=level.upper(), message=message, ts=time.time())
