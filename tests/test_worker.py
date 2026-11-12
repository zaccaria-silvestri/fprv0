import threading
import time

import pytest

from fprv0.core.bus import DATA, STATUS, EventBus
from fprv0.core.commands import CommandError, collect_commands, command
from fprv0.core.worker import InstrumentWorker, NotConnectedError
from tests.conftest import wait_for


class FakeDriver:
    """Driver scriptable : pannes de connexion / de mesure à la demande."""

    def __init__(self):
        self.fail_connect = False
        self.fail_poll = False
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.polls = 0
        self.command_threads: list[str] = []

    @classmethod
    def commands(cls):
        return collect_commands(cls)

    def connect(self):
        self.connect_calls += 1
        if self.fail_connect:
            raise ConnectionError("appareil injoignable")

    def poll(self):
        if self.fail_poll:
            raise TimeoutError("pas de réponse")
        self.polls += 1
        return {"timestamp": time.time(), "n": self.polls}

    def disconnect(self):
        self.disconnect_calls += 1

    def execute(self, name, kwargs):
        return getattr(self, name)(**self.commands()[name].validate(kwargs))

    @command("Écho")
    def echo(self, value: int):
        self.command_threads.append(threading.current_thread().name)
        return {"echo": value}

    @command("Boum")
    def boom(self):
        raise RuntimeError("l'appareil a refusé")


@pytest.fixture
def rig():
    bus = EventBus()
    states: list[str] = []
    received: list[dict] = []
    bus.subscribe(STATUS, lambda inst_id, status: states.append(status["state"]))
    bus.subscribe(DATA, lambda inst_id, data: received.append(data))
    workers = []

    def make(driver=None, **kwargs):
        kwargs.setdefault("poll_interval", 0.02)
        kwargs.setdefault("retry_min", 0.02)
        kwargs.setdefault("retry_max", 0.05)
        worker = InstrumentWorker("fake", driver or FakeDriver(), bus, **kwargs)
        workers.append(worker)
        return worker

    yield make, states, received
    for w in workers:
        w.request_stop()
        if w.is_alive():
            w.join(2)


def test_connects_and_publishes_data(rig):
    make, states, data = rig
    worker = make()
    worker.start()
    wait_for(lambda: len(data) >= 3)
    assert worker.snapshot()["state"] == "connected"
    assert states[:2] == ["connecting", "connected"]
    assert worker.snapshot()["last_ok"] is not None


def test_initial_connection_retries_with_backoff(rig):
    make, states, data = rig
    driver = FakeDriver()
    driver.fail_connect = True
    worker = make(driver)
    worker.start()
    wait_for(lambda: driver.connect_calls >= 3)
    assert worker.snapshot()["state"] in ("disconnected", "connecting")
    assert data == []
    driver.fail_connect = False
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    wait_for(lambda: len(data) >= 1)
    assert "disconnected" in states


def test_single_poll_failure_is_degraded_then_recovers(rig):
    make, states, data = rig
    driver = FakeDriver()
    worker = make(driver, max_failures=50)
    worker.start()
    wait_for(lambda: len(data) >= 1)
    driver.fail_poll = True
    wait_for(lambda: worker.snapshot()["state"] == "degraded")
    assert "TimeoutError" in worker.snapshot()["detail"]
    driver.fail_poll = False
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    assert worker.snapshot()["failures"] == 0
    assert driver.connect_calls == 1  # pas de reconnexion pour un simple raté


def test_repeated_failures_trigger_reconnection(rig):
    make, states, data = rig
    driver = FakeDriver()
    worker = make(driver, max_failures=3)
    worker.start()
    wait_for(lambda: len(data) >= 1)
    driver.fail_poll = True
    wait_for(lambda: "disconnected" in states)
    driver.fail_poll = False
    wait_for(lambda: driver.connect_calls >= 2 and worker.snapshot()["state"] == "connected")
    assert driver.disconnect_calls >= 1
    assert worker.snapshot()["reconnects"] == 1


def test_commands_run_in_worker_thread_and_return_result(rig):
    make, states, data = rig
    driver = FakeDriver()
    worker = make(driver)
    worker.start()
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    result = worker.submit_command("echo", {"value": 7}).result(timeout=2)
    assert result == {"echo": 7}
    assert driver.command_threads == ["worker-fake"]  # jamais dans le thread appelant


def test_command_failure_is_reported_and_polling_continues(rig):
    make, states, data = rig
    worker = make()
    worker.start()
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    with pytest.raises(RuntimeError, match="refusé"):
        worker.submit_command("boom").result(timeout=2)
    n = len(data)
    wait_for(lambda: len(data) > n)


def test_invalid_command_is_rejected_synchronously(rig):
    make, *_ = rig
    worker = make()
    worker.start()
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    with pytest.raises(CommandError, match="inconnue"):
        worker.submit_command("connect")
    with pytest.raises(CommandError):
        worker.submit_command("echo", {"value": "pas un entier"})


def test_commands_are_refused_when_not_connected(rig):
    make, *_ = rig
    never_started = make()
    with pytest.raises(NotConnectedError):
        never_started.submit_command("echo", {"value": 1})

    driver = FakeDriver()
    driver.fail_connect = True
    worker = make(driver)
    worker.start()
    wait_for(lambda: driver.connect_calls >= 1)
    with pytest.raises(NotConnectedError):
        worker.submit_command("echo", {"value": 1})
    assert driver.command_threads == []  # rien n'est exécuté « plus tard » à la reconnexion


def test_stop_is_immediate_even_with_long_poll_interval(rig):
    make, *_ = rig
    worker = make(poll_interval=30.0)
    worker.start()
    wait_for(lambda: worker.snapshot()["state"] == "connected")
    t0 = time.monotonic()
    worker.request_stop()
    worker.join(2)
    assert not worker.is_alive()
    assert time.monotonic() - t0 < 1.0
    assert worker.snapshot()["state"] == "stopped"
    assert worker.driver.disconnect_calls == 1


def test_stop_interrupts_backoff_wait(rig):
    make, *_ = rig
    driver = FakeDriver()
    driver.fail_connect = True
    worker = make(driver, retry_min=30.0, retry_max=30.0)
    worker.start()
    wait_for(lambda: worker.snapshot()["state"] == "disconnected")
    t0 = time.monotonic()
    worker.request_stop()
    worker.join(2)
    assert not worker.is_alive() and time.monotonic() - t0 < 1.0


def test_poll_cadence_does_not_drift_or_burst(rig):
    make, states, data = rig
    driver = FakeDriver()
    worker = make(driver, poll_interval=0.05)
    worker.start()
    time.sleep(0.52)
    count = len(data)
    assert 8 <= count <= 12  # ~10 attendus : ni retard cumulé, ni rafale
