import json

import pytest

from fprv0.config import load_config
from fprv0.core.bus import DATA, EventBus
from fprv0.core.manager import InstrumentManager
from fprv0.core.store import DataStore
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

[dead_meter]
type = "keithley_2000"
transport = "http"
address = "http://127.0.0.1:9/api"
poll_interval_s = 0.05
timeout_s = 0.3
retry_min_s = 30
retry_max_s = 30
"""


class Stack:
    def __init__(self, tmp_path, token):
        app_toml = '[app]\nsave_csv = false\n' + (f'api_token = "{token}"\n' if token else "")
        (tmp_path / "app.toml").write_text(app_toml)
        (tmp_path / "instruments.toml").write_text(SIM)
        self.config = load_config(tmp_path)
        self.bus = EventBus()
        self.store = DataStore(self.bus, 100)
        self.broker = SSEBroker(self.bus)
        self.manager = InstrumentManager(self.bus, self.config)
        self.manager.start_all()
        self.client = create_app(self.manager, self.store, self.broker, self.config).test_client()
        wait_for(lambda: {"eip_counter", "swagelok_valves"} <= set(self.store.latest()))
        wait_for(lambda: self.manager.workers["dead_meter"].snapshot()["state"] == "disconnected")

    def post(self, payload, token=None, **kwargs):
        headers = {"X-API-Token": token} if token else {}
        return self.client.post("/api/action", json=payload, headers=headers, **kwargs)


@pytest.fixture
def stack(tmp_path):
    s = Stack(tmp_path, token="s3cret")
    yield s
    s.manager.stop_all()


@pytest.fixture
def open_stack(tmp_path):  # sans jeton (écoute locale)
    s = Stack(tmp_path, token=None)
    yield s
    s.manager.stop_all()


VALVE = {"instrument_id": "swagelok_valves", "action": "set_vanne_state", "kwargs": {"index": 0, "state": 1}}


# --------------------------------------------------------------------------- description pilotée par la config

def test_instruments_endpoint_describes_ui_and_commands(stack):
    body = stack.client.get("/api/instruments").get_json()
    assert body["app"]["auth_required"] is True and body["app"]["simulation"] is True
    by_id = {i["id"]: i for i in body["instruments"]}
    assert set(by_id) == {"eip_counter", "swagelok_valves", "dead_meter"}
    assert by_id["eip_counter"]["ui"]["kind"] == "readings"
    assert by_id["swagelok_valves"]["ui"]["kind"] == "valves"
    commands = {c["name"]: c for c in by_id["swagelok_valves"]["commands"]}
    assert commands["toggle_all"]["dangerous"] is True and commands["set_vanne_state"]["dangerous"] is False
    assert by_id["eip_counter"]["commands"] == []
    assert by_id["dead_meter"]["transport"] == "http"


def test_status_latest_history_and_journal(stack):
    status = stack.client.get("/api/status").get_json()
    assert status["eip_counter"]["state"] == "connected"
    assert status["dead_meter"]["state"] == "disconnected"
    assert "eip_counter" in stack.client.get("/api/latest").get_json()

    hist = stack.client.get("/api/history?instrument=eip_counter&limit=5&keys=timestamp,freq").get_json()
    assert 0 < len(hist["points"]) <= 5
    assert set(hist["points"][0]) == {"timestamp", "freq"}
    last = hist["points"][-1]["timestamp"]
    later = stack.client.get(f"/api/history?instrument=eip_counter&since={last}").get_json()["points"]
    assert all(p["timestamp"] > last for p in later)
    assert stack.client.get("/api/history?instrument=nope").status_code == 404
    assert stack.client.get("/api/history?instrument=eip_counter&limit=abc").status_code == 400
    assert any("dead_meter" in e["message"] for e in stack.client.get("/api/journal").get_json())


def test_security_headers(stack):
    response = stack.client.get("/")
    assert response.status_code == 200
    assert response.headers["X-Frame-Options"] == "DENY"
    assert "default-src 'self'" in response.headers["Content-Security-Policy"]
    assert b"cdn.jsdelivr" not in response.data  # plus aucune dépendance externe


# --------------------------------------------------------------------------- authentification

def test_action_requires_token(stack):
    assert stack.post(VALVE).status_code == 401
    assert stack.post(VALVE, token="wrong").status_code == 401
    assert stack.post(VALVE, token="s3cret").status_code == 200


def test_bearer_token_is_accepted(stack):
    response = stack.client.post("/api/action", json=VALVE, headers={"Authorization": "Bearer s3cret"})
    assert response.status_code == 200


def test_reads_stay_open_without_token(stack):
    assert stack.client.get("/api/latest").status_code == 200


def test_no_token_configured_allows_local_use(open_stack):
    assert open_stack.post(VALVE).status_code == 200


# --------------------------------------------------------------------------- liste blanche et validation

@pytest.mark.parametrize("action", ["connect", "disconnect", "poll", "execute", "fetch_raw", "__init__", "_reboot", "transport"])
def test_non_command_methods_are_not_callable(stack, action):
    response = stack.post({"instrument_id": "swagelok_valves", "action": action}, token="s3cret")
    assert response.status_code == 404
    assert response.get_json()["error"] == "unknown_command"


def test_unknown_instrument_and_bad_bodies(stack):
    assert stack.post({"instrument_id": "nope", "action": "reboot"}, token="s3cret").status_code == 404
    assert stack.post({"action": "reboot"}, token="s3cret").status_code == 404
    assert stack.client.post("/api/action", data="x=1", headers={"X-API-Token": "s3cret"}).status_code == 400
    assert stack.client.post("/api/action", data="[1,2]", content_type="application/json",
                             headers={"X-API-Token": "s3cret"}).status_code == 400


def test_parameter_validation_returns_400(stack):
    bad = {**VALVE, "kwargs": {"index": 99, "state": 1}}
    response = stack.post(bad, token="s3cret")
    assert response.status_code == 400 and response.get_json()["error"] == "invalid_parameters"
    assert stack.post({**VALVE, "kwargs": {"index": 0, "state": 1, "x": 1}}, token="s3cret").status_code == 400


def test_valve_command_really_changes_state(stack):
    assert stack.post(VALVE, token="s3cret").get_json()["status"] == "ok"
    wait_for(lambda: stack.store.latest()["swagelok_valves"]["vannes"][0] == 1)
    stack.post({**VALVE, "kwargs": {"index": 0, "state": 0}}, token="s3cret")
    wait_for(lambda: stack.store.latest()["swagelok_valves"]["vannes"][0] == 0)


def test_dangerous_commands_need_confirmation(stack):
    payload = {"instrument_id": "swagelok_valves", "action": "toggle_all", "kwargs": {"state": 1}}
    response = stack.post(payload, token="s3cret")
    assert response.status_code == 428 and response.get_json()["error"] == "confirmation_required"
    assert stack.post({**payload, "confirmed": "true"}, token="s3cret").status_code == 428  # vrai booléen exigé
    assert stack.post({**payload, "confirmed": True}, token="s3cret").status_code == 200
    wait_for(lambda: stack.store.latest()["swagelok_valves"]["vannes"][:4] == [1, 1, 1, 1])
    assert stack.post({"instrument_id": "swagelok_valves", "action": "reboot", "confirmed": True}, token="s3cret").status_code == 200


def test_command_on_unreachable_instrument_returns_409(stack):
    response = stack.post({"instrument_id": "dead_meter", "action": "reboot"}, token="s3cret")
    assert response.status_code == 404  # le Keithley n'expose aucune commande
    # et un instrument qui a des commandes mais est hors ligne :
    worker = stack.manager.workers["swagelok_valves"]
    worker.request_stop()
    worker.join(2)
    response = stack.post(VALVE, token="s3cret")
    assert response.status_code == 409 and response.get_json()["error"] == "not_connected"


# --------------------------------------------------------------------------- diffuseur SSE

def test_broker_delivers_to_every_client():
    bus = EventBus()
    broker = SSEBroker(bus)
    first, second = broker.stream(), broker.stream()
    assert next(first) == ": connected\n\n" and next(second) == ": connected\n\n"
    assert broker.client_count == 2

    bus.publish(DATA, inst_id="eip", data={"freq": 1.0, "timestamp": 5.0})
    for client in (first, second):  # l'ancienne file partagée n'en servait qu'un seul
        message = next(client)
        assert message.startswith("event: data\ndata: ") and message.endswith("\n\n")
        assert json.loads(message.split("data: ", 1)[1]) == {"instrument": "eip", "data": {"freq": 1.0, "timestamp": 5.0}}

    first.close()
    assert broker.client_count == 1
    broker.close()
    assert list(second) == []  # le flux se termine proprement à l'arrêt


def test_slow_client_drops_oldest_instead_of_blocking():
    bus = EventBus()
    broker = SSEBroker(bus, queue_size=3)
    client = broker.stream()
    next(client)
    for i in range(10):
        bus.publish(DATA, inst_id="eip", data={"i": i})  # ne doit jamais bloquer
    received = [json.loads(next(client).split("data: ", 1)[1])["data"]["i"] for _ in range(3)]
    assert received == [7, 8, 9]
    broker.close()


# --------------------------------------------------------------------------- point d'entrée

def test_main_refuses_network_binding_without_token(tmp_path, capsys):
    from fprv0.__main__ import main

    (tmp_path / "instruments.toml").write_text('[a]\ntype="eip_25b"\ntransport="sim"\naddress="x"\n')
    assert main(["--config-dir", str(tmp_path), "--host", "0.0.0.0"]) == 2
    assert "jeton" in capsys.readouterr().err


def test_main_reports_unknown_driver_type(tmp_path, capsys):
    from fprv0.__main__ import main

    (tmp_path / "instruments.toml").write_text('[a]\ntype="eip_26b"\naddress="x"\n')
    assert main(["--config-dir", str(tmp_path)]) == 2
    assert "eip_26b" in capsys.readouterr().err
