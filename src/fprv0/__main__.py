"""Point d'entrée : ``fprv0 [--sim|--no-sim] [--config-dir DIR] [--host H] [--port P]``."""
from __future__ import annotations

import argparse
import logging
import signal
import sqlite3
import sys

from fprv0.config import ConfigError, ensure_safe_binding, load_config
from fprv0.core.bus import DATA, LOG, EventBus
from fprv0.core.manager import InstrumentManager
from fprv0.core.store import DataStore
from fprv0.data.history import HistoryDB
from fprv0.data.recorder import CSVRecorder
from fprv0.web.app import run_web_server
from fprv0.web.broker import SSEBroker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="fprv0", description="fprv0 - pilotage d'instruments et acquisition")
    parser.add_argument(
        "--sim",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--sim force la simulation de tous les instruments, --no-sim force le matériel réel "
        "(sans l'option : valeur de simulation_mode dans app.toml, puis 'transport' de chaque instrument)",
    )
    parser.add_argument("--config-dir", default=None, help="Dossier contenant app.toml et instruments.toml")
    parser.add_argument("--host", default=None, help="Adresse d'écoute (défaut : web_host de app.toml)")
    parser.add_argument("--port", type=int, default=None, help="Port d'écoute (défaut : web_port de app.toml)")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=args.log_level, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("werkzeug").setLevel(logging.WARNING)

    bus = EventBus()
    try:
        config = load_config(args.config_dir, sim=args.sim, host=args.host, port=args.port)
        ensure_safe_binding(config)
        manager = InstrumentManager(bus, config)
    except ConfigError as exc:
        print(f"Erreur de configuration : {exc}", file=sys.stderr)
        return 2

    store = DataStore(bus, config.app.history_points)
    broker = SSEBroker(bus)
    recorder = CSVRecorder(config.app.data_dir) if config.app.save_csv else None
    if recorder:
        bus.subscribe(DATA, recorder.on_data)

    history = None
    if config.app.history_db:
        try:
            history = HistoryDB(
                config.app.data_dir / config.app.db_file,
                extractors={inst_id: type(w.driver).history_values for inst_id, w in manager.workers.items()},
                period_s=config.app.db_period_s,
                retention_days=config.app.db_retention_days,
            )
        except (OSError, sqlite3.Error):
            # L'acquisition prime : sans base on continue (mémoire + CSV), mais on le dit clairement.
            logging.getLogger("fprv0").exception("Base de données historique indisponible : fonctionnement sans")
        else:
            store.preload_journal(history.recent_journal())  # le journal survit aux redémarrages
            bus.subscribe(DATA, history.on_data)
            bus.subscribe(LOG, history.on_log)
            history.start()

    # SIGTERM (systemd, docker stop) se comporte comme Ctrl+C : arrêt propre.
    def _terminate(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _terminate)

    mode = "SIMULATION" if manager.sim_mode else "matériel réel"
    logging.getLogger("fprv0").info("Démarrage (%s), %d instrument(s)", mode, len(manager.workers))
    manager.start_all()
    try:
        run_web_server(manager, store, broker, config, history)
    finally:
        manager.stop_all()
        if history:
            history.close()
        if recorder:
            recorder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
