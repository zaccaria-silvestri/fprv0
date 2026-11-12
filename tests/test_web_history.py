import time

import pytest

from fprv0.config import load_config
from fprv0.core.bus import DATA, LOG, EventBus
from fprv0.core.manager import InstrumentManager
from fprv0.core.store import DataStore
from fprv0.data.history import HistoryDB
from fprv0.web.app import create_app
from fprv0.web.broker import SSEBroker
from tests.conftest import wait_for

SIM = """
[eip_counter]
type = "eip_25b"
transport = "sim"
address = "http://sim/api"
poll_interval_s = 0.02

[swagelok_valves]
type = "swagelok_dp"
transport = "sim"
address = "http://sim/api/status"
poll_interval_s = 0.02
"""


class Stack:
    def __init__(self, tmp_path, with_db=True):
        (tmp_path / "app.toml").write_text('[app]\nsave_csv = false\napi_token = "tok"\ndb_period_s = 1\ndb_retention_days = 30\n')
        (tmp_path / "instruments.toml").write_text(SIM)
        self.config = load_config(tmp_path)
        self.bus = EventBus()
        self.store = DataStore(self.bus, 5000)
        self.broker = SSEBroker(self.bus)
        self.manager = InstrumentManager(self.bus, self.config)
        self.history = None
        if with_db:
            self.history = HistoryDB(
                tmp_path / "h.db",
                extractors={i: type(w.driver).history_values for i, w in self.manager.workers.items()},
                period_s=1,
                retention_days=30,
            )
            self.bus.subscribe(DATA, self.history.on_data)
            self.bus.subscribe(LOG, self.history.on_log)
        self.manager.start_all()
        self.client = create_app(self.manager, self.store, self.broker, self.config, self.history).test_client()
        wait_for(lambda: {"eip_counter", "swagelok_valves"} <= set(self.store.latest()))

    def close(self):
        self.manager.stop_all()
        if self.history:
            self.history.close()


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path)
    yield s
    s.close()


@pytest.fixture
def no_db_stack(tmp_path):
    s = Stack(tmp_path, with_db=False)
    yield s
    s.close()


def test_instruments_advertises_history_capabilities(stack, no_db_stack):
    info = stack.client.get("/api/instruments").get_json()["app"]["history"]
    assert info == {"enabled": True, "period_s": 1, "retention_hours": 720}
    assert no_db_stack.client.get("/api/instruments").get_json()["app"]["history"]["enabled"] is False


def test_history_by_period_merges_memory_and_database(stack):
    stack.history.insert_samples(
        [("eip_counter", "freq", int((time.time() - 2 * 3600 + i * 60) * 1000), 42.0) for i in range(60)]
    )
    body = stack.client.get("/api/history?instrument=eip_counter&hours=3&max_points=500").get_json()
    assert body["from_db"] is True and body["hours"] == 3
    assert set(body["points"][0]) == {"timestamp", "freq"}  # par défaut : la mesure du graphique
    assert any(p["freq"] == 42.0 for p in body["points"]) and any(p["freq"] and p["freq"] > 1e6 for p in body["points"])
    assert len(body["points"]) <= 500


def test_history_by_period_accepts_the_keys_sent_by_the_browser(stack):
    stack.history.insert_samples(
        [("eip_counter", "freq", int((time.time() - 2 * 3600 + i * 60) * 1000), 42.0) for i in range(60)]
    )
    url = "/api/history?instrument=eip_counter&hours=3&max_points=1500&keys=timestamp,freq"
    body = stack.client.get(url).get_json()
    assert body["from_db"] is True
    assert all(isinstance(p["timestamp"], float) for p in body["points"])  # même requête que le front : plus de 500


def test_history_by_period_validates_and_clamps(stack):
    assert stack.client.get("/api/history?instrument=eip_counter&hours=abc").status_code == 400
    assert stack.client.get("/api/history?instrument=nope&hours=1").status_code == 404
    body = stack.client.get("/api/history?instrument=eip_counter&hours=99999").get_json()
    assert body["hours"] == 720  # plafonné à la rétention


def test_history_by_period_works_without_database(no_db_stack):
    body = no_db_stack.client.get("/api/history?instrument=eip_counter&hours=1").get_json()
    assert body["from_db"] is False and len(body["points"]) > 0


def test_csv_export_is_excel_friendly(stack):
    stack.history.insert_samples(
        [("eip_counter", "freq", int((time.time() - 600) * 1000), 1.5), ("eip_counter", "temp", int((time.time() - 600) * 1000), 2.25)]
    )
    response = stack.client.get("/api/export.csv?instrument=eip_counter&hours=1")
    assert response.status_code == 200
    assert "attachment" in response.headers["Content-Disposition"]
    text = response.data.decode("utf-8")
    assert text.startswith("\ufeff")  # BOM : Excel lit l'UTF-8
    lines = text.lstrip("\ufeff").splitlines()
    assert lines[0] == "date;freq;temp"
    assert lines[1].endswith(";1,5;2,25")  # virgule décimale, séparateur « ; »


def test_csv_export_errors(stack, no_db_stack):
    assert stack.client.get("/api/export.csv?instrument=nope").status_code == 404
    assert no_db_stack.client.get("/api/export.csv?instrument=eip_counter").status_code == 404


def test_commands_are_audited_with_remote_address(stack):
    headers = {"X-API-Token": "tok"}
    ok = {"instrument_id": "swagelok_valves", "action": "set_vanne_state", "kwargs": {"index": 0, "state": 1}}
    assert stack.client.post("/api/action", json=ok, headers=headers).status_code == 200
    assert stack.client.post("/api/action", json={"instrument_id": "swagelok_valves", "action": "poll"}, headers=headers).status_code == 404
    stack.history.flush()

    entries = stack.client.get("/api/audit").get_json()
    assert len(entries) == 1  # seules les commandes réellement tentées sont tracées
    entry = entries[0]
    assert entry["instrument"] == "swagelok_valves" and entry["action"] == "set_vanne_state"
    assert entry["params"] == {"index": 0, "state": 1} and entry["outcome"] == "ok"
    assert entry["remote"] == "127.0.0.1"


def test_refused_command_on_offline_instrument_is_audited(stack):
    worker = stack.manager.workers["swagelok_valves"]
    worker.request_stop()
    worker.join(2)
    body = {"instrument_id": "swagelok_valves", "action": "reboot", "confirmed": True}
    assert stack.client.post("/api/action", json=body, headers={"X-API-Token": "tok"}).status_code == 409
    stack.history.flush()
    assert "hors ligne" in stack.client.get("/api/audit").get_json()[0]["outcome"]


def test_audit_endpoint_disabled_without_database(no_db_stack):
    assert no_db_stack.client.get("/api/audit").status_code == 404


def test_journal_is_persisted_and_preloaded(tmp_path):
    first = Stack(tmp_path)
    first.history.flush()
    saved = first.history.recent_journal()
    first.close()
    assert any("connected" in e["message"] for e in saved)

    bus = EventBus()
    store = DataStore(bus)
    db = HistoryDB(tmp_path / "h.db", extractors={}, period_s=1)
    store.preload_journal(db.recent_journal())
    bus.log("INFO", "nouveau message")
    messages = [e["message"] for e in store.journal()]
    assert messages[-1] == "nouveau message" and len(messages) > 1  # ancien journal d'abord, nouveau ensuite
