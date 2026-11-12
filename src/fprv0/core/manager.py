"""Création et pilotage du cycle de vie des workers à partir de la configuration."""
from __future__ import annotations

import logging

import fprv0.instruments  # noqa: F401  (enregistre les drivers connus)
from fprv0.config import Config, ConfigError
from fprv0.core.bus import EventBus
from fprv0.core.registry import create_instrument
from fprv0.core.worker import InstrumentWorker

logger = logging.getLogger(__name__)


class InstrumentManager:
    def __init__(self, bus: EventBus, config: Config) -> None:
        self.bus = bus
        self.config = config
        self.workers: dict[str, InstrumentWorker] = {}

        for inst_id, cfg in config.instruments.items():
            if not cfg.enabled:
                continue
            try:
                driver = create_instrument(cfg.type, cfg.transport, cfg.address, timeout=cfg.timeout_s)
            except ValueError as exc:
                # Une faute de frappe dans la config doit être bloquante, pas silencieuse.
                raise ConfigError(f"[{inst_id}] {exc}") from exc
            self.workers[inst_id] = InstrumentWorker(
                inst_id,
                driver,
                bus,
                poll_interval=cfg.poll_interval_s,
                retry_min=cfg.retry_min_s,
                retry_max=cfg.retry_max_s,
                max_failures=cfg.max_failures,
            )

    @property
    def sim_mode(self) -> bool:
        return any(cfg.transport == "sim" for cfg in self.config.instruments.values())

    def start_all(self) -> None:
        for worker in self.workers.values():
            worker.start()

    def stop_all(self, timeout: float = 5.0) -> None:
        """Demande l'arrêt de tous les workers en parallèle, puis attend leur fin."""
        for worker in self.workers.values():
            worker.request_stop()
        for worker in self.workers.values():
            if worker.is_alive():
                worker.join(timeout)
                if worker.is_alive():
                    logger.warning("Le worker '%s' ne s'est pas arrêté dans les %.0f s", worker.instrument_id, timeout)

    def statuses(self) -> dict[str, dict]:
        return {inst_id: worker.snapshot() for inst_id, worker in self.workers.items()}
