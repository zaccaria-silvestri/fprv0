"""Diffusion Server-Sent Events vers N navigateurs.

Chaque client a sa propre file (l'ancienne version partageait une file unique : chaque
message n'arrivait qu'à UN navigateur). Un client trop lent perd ses plus anciens messages
au lieu de bloquer les workers.
"""
from __future__ import annotations

import json
import queue
import threading
from collections.abc import Iterator

from fprv0.core.bus import DATA, LOG, STATUS, EventBus

HEARTBEAT_S = 15.0


def format_sse(event: str, payload: object) -> str:
    return f"event: {event}\ndata: {json.dumps(payload, allow_nan=False)}\n\n"


class SSEBroker:
    def __init__(self, bus: EventBus, queue_size: int = 1000) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()
        self._queue_size = queue_size
        self._closed = False
        bus.subscribe(DATA, lambda inst_id, data: self._broadcast("data", {"instrument": inst_id, "data": data}))
        bus.subscribe(STATUS, lambda inst_id, status: self._broadcast("status", {"instrument": inst_id, "status": status}))
        bus.subscribe(LOG, lambda level, message, ts: self._broadcast("log", {"level": level, "message": message, "ts": ts}))

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _broadcast(self, event: str, payload: object) -> None:
        message = format_sse(event, payload)
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(message)
            except queue.Full:
                try:  # client lent : on jette le plus ancien message
                    client.get_nowait()
                    client.put_nowait(message)
                except (queue.Empty, queue.Full):
                    pass

    def stream(self) -> Iterator[str]:
        client: queue.Queue = queue.Queue(maxsize=self._queue_size)
        with self._lock:
            if self._closed:
                return
            self._clients.add(client)
        try:
            yield ": connected\n\n"
            while True:
                try:
                    message = client.get(timeout=HEARTBEAT_S)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if message is None:
                    return
                yield message
        finally:
            with self._lock:
                self._clients.discard(client)

    def close(self) -> None:
        """Termine proprement tous les flux ouverts (arrêt du serveur)."""
        with self._lock:
            self._closed = True
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(None)
            except queue.Full:
                try:
                    client.get_nowait()
                    client.put_nowait(None)
                except (queue.Empty, queue.Full):
                    pass
