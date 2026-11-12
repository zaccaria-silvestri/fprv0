"""Application Flask : tableau de bord + API.

Sécurité :
* l'API de commande n'accepte que les commandes déclarées par les drivers (``@command``) ;
* ``POST /api/action`` exige le jeton d'API quand il est configuré (en-tête ``X-API-Token`` ou
  ``Authorization: Bearer``) ; la configuration refuse d'écouter hors boucle locale sans jeton ;
* les commandes ``dangerous`` exigent ``"confirmed": true`` (la modale du navigateur le fournit) ;
* le corps doit être du JSON (``Content-Type: application/json``), ce qui force une requête
  « preflight » CORS et bloque les appels forgés depuis un autre site ;
* les lectures (``GET``) restent ouvertes : l'EventSource du navigateur ne sait pas envoyer d'en-tête.
"""
from __future__ import annotations

import csv
import hmac
import io
import logging
import time
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime

from flask import Flask, Response, jsonify, render_template, request
from werkzeug.serving import make_server

from fprv0.config import Config
from fprv0.core.commands import CommandError
from fprv0.core.manager import InstrumentManager
from fprv0.core.store import DataStore
from fprv0.core.worker import NotConnectedError
from fprv0.data.history import HistoryDB, build_history
from fprv0.web.broker import SSEBroker

logger = logging.getLogger(__name__)

ACTION_TIMEOUT_S = 8.0
MAX_HISTORY_POINTS = 20000
CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"


def create_app(
    manager: InstrumentManager,
    store: DataStore,
    broker: SSEBroker,
    config: Config,
    history: HistoryDB | None = None,
) -> Flask:
    app = Flask(__name__)
    token = config.app.api_token
    retention_hours = config.app.db_retention_days * 24

    def history_keys(inst_id: str) -> list[str]:
        """Mesures à tracer par défaut : celle du graphique, sinon les mesures affichées."""
        ui = manager.workers[inst_id].driver.ui
        if ui.get("plot"):
            return [ui["plot"]["key"]]
        return [r["key"] for r in ui.get("readings", [])]

    def audit(inst_id: str, action: str, params: dict, outcome: str) -> None:
        if history is not None:
            history.record_audit(request.remote_addr, inst_id, action, params, outcome)

    def describe(inst_id: str) -> dict:
        worker = manager.workers[inst_id]
        cfg = config.instruments[inst_id]
        ui = dict(worker.driver.ui)
        return {
            "id": inst_id,
            "type": cfg.type,
            "transport": cfg.transport,
            "label": cfg.label or ui.get("title", inst_id),
            "poll_interval_s": cfg.poll_interval_s,
            "ui": ui,
            "commands": [spec.to_dict() for spec in worker.driver.commands().values()],
        }

    def authorized() -> bool:
        if not token:
            return True
        supplied = request.headers.get("X-API-Token", "")
        auth = request.headers.get("Authorization", "")
        if not supplied and auth.lower().startswith("bearer "):
            supplied = auth[7:].strip()
        return hmac.compare_digest(supplied.encode(), token.encode())

    @app.after_request
    def security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    @app.get("/")
    def index():
        return render_template("index.html", app_name=config.app.name)

    @app.get("/api/instruments")
    def get_instruments():
        return jsonify(
            app={
                "name": config.app.name,
                "simulation": manager.sim_mode,
                "auth_required": bool(token),
                "history_points": config.app.history_points,
                "history": {
                    "enabled": history is not None,
                    "period_s": config.app.db_period_s,
                    "retention_hours": retention_hours,
                },
            },
            instruments=[describe(inst_id) for inst_id in manager.workers],
        )

    @app.get("/api/status")
    def get_status():
        return jsonify(manager.statuses())

    @app.get("/api/latest")
    def get_latest():
        return jsonify(store.latest())

    @app.get("/api/journal")
    def get_journal():
        return jsonify(store.journal())

    @app.get("/api/history")
    def get_history():
        inst_id = request.args.get("instrument", "")
        if inst_id not in manager.workers:
            return jsonify(error="unknown_instrument"), 404
        try:
            limit = min(int(request.args.get("limit", 2000)), MAX_HISTORY_POINTS)
            since = float(request.args["since"]) if "since" in request.args else None
        except ValueError:
            return jsonify(error="bad_parameter"), 400
        keys = [k for k in request.args.get("keys", "").split(",") if k] or None

        if "hours" in request.args:  # historique sur une période : mémoire + base de données
            try:
                hours = min(max(float(request.args["hours"]), 0.01), retention_hours if history else 24)
                max_points = min(max(int(request.args.get("max_points", 1500)), 100), 5000)
            except ValueError:
                return jsonify(error="bad_parameter"), 400
            now = time.time()
            result = build_history(
                store.history(inst_id),
                history,
                inst_id,
                keys or history_keys(inst_id),
                since=now - hours * 3600,
                now=now,
                max_points=max_points,
            )
            return jsonify(instrument=inst_id, hours=hours, **result)

        return jsonify(instrument=inst_id, points=store.history(inst_id, since=since, limit=limit, keys=keys))

    @app.get("/api/audit")
    def get_audit():
        if history is None:
            return jsonify(error="history_disabled"), 404
        return jsonify(history.recent_audit(min(request.args.get("limit", 100, type=int), 1000)))

    @app.get("/api/export.csv")
    def export_csv():
        """Moyennes enregistrées d'un instrument (Excel FR : séparateur « ; », virgule décimale, BOM UTF-8)."""
        if history is None:
            return jsonify(error="history_disabled", detail="Base de données historique désactivée."), 404
        inst_id = request.args.get("instrument", "")
        if inst_id not in manager.workers:
            return jsonify(error="unknown_instrument"), 404
        hours = min(max(request.args.get("hours", 24, type=float), 0.01), retention_hours)
        rows = history.export_rows(inst_id, time.time() - hours * 3600)
        columns = sorted({key for _, values in rows for key in values})

        def cell(value):
            return "" if value is None else str(round(value, 6)).replace(".", ",")

        buffer = io.StringIO()
        writer = csv.writer(buffer, delimiter=";", lineterminator="\n")
        writer.writerow(["date", *columns])
        for ts, values in rows:
            stamp = datetime.fromtimestamp(ts).isoformat(sep=" ", timespec="seconds")
            writer.writerow([stamp, *(cell(values.get(c)) for c in columns)])
        return Response(
            "\ufeff" + buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{inst_id}_{hours:g}h.csv"'},
        )

    @app.get("/api/stream")
    def stream():
        return Response(
            broker.stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/action")
    def trigger_action():
        if not authorized():
            return jsonify(error="unauthorized", detail="Jeton d'API manquant ou invalide."), 401

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="bad_request", detail="Corps JSON attendu (Content-Type: application/json)."), 400

        inst_id = payload.get("instrument_id")
        action = payload.get("action")
        worker = manager.workers.get(inst_id) if isinstance(inst_id, str) else None
        if worker is None:
            return jsonify(error="unknown_instrument", detail=f"Instrument inconnu : {inst_id!r}"), 404

        spec = worker.driver.commands().get(action) if isinstance(action, str) else None
        if spec is None:  # liste blanche : seules les méthodes marquées @command sont appelables
            return jsonify(error="unknown_command", detail=f"Commande inconnue : {action!r}"), 404

        if spec.dangerous and payload.get("confirmed") is not True:
            return jsonify(error="confirmation_required", detail=f"« {spec.label} » demande une confirmation."), 428

        try:
            future = worker.submit_command(action, payload.get("kwargs") or {})
        except CommandError as exc:
            return jsonify(error="invalid_parameters", detail=str(exc)), 400
        except NotConnectedError as exc:
            audit(inst_id, action, payload.get("kwargs") or {}, "refusée : instrument hors ligne")
            return jsonify(error="not_connected", detail=str(exc)), 409

        params = payload.get("kwargs") or {}
        try:
            result = future.result(timeout=ACTION_TIMEOUT_S)
        except FutureTimeout:
            audit(inst_id, action, params, "sans réponse")
            return jsonify(error="timeout", detail="L'instrument n'a pas répondu à temps."), 504
        except Exception as exc:  # noqa: BLE001 - l'erreur vient de l'appareil
            audit(inst_id, action, params, f"échec : {type(exc).__name__}: {exc}")
            return jsonify(error="command_failed", detail=f"{type(exc).__name__}: {exc}"), 502
        audit(inst_id, action, params, "ok")
        return jsonify(status="ok", result=result)

    return app


def run_web_server(
    manager: InstrumentManager,
    store: DataStore,
    broker: SSEBroker,
    config: Config,
    history: HistoryDB | None = None,
) -> None:
    """Sert l'application dans le thread courant jusqu'à Ctrl+C / SIGTERM."""
    app = create_app(manager, store, broker, config, history)
    host, port = config.app.web_host, config.app.web_port
    server = make_server(host, port, app, threaded=True)
    logger.info("Tableau de bord sur http://%s:%d", host, port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Arrêt demandé")
    finally:
        broker.close()
        server.server_close()
