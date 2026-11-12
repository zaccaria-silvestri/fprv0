import time

import pytest


def wait_for(predicate, timeout: float = 3.0, interval: float = 0.01):
    """Attend qu'une condition devienne vraie ; échoue proprement sinon."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError("condition non remplie dans le délai imparti")


@pytest.fixture(autouse=True)
def _no_ambient_env(monkeypatch):
    monkeypatch.delenv("FPRV0_API_TOKEN", raising=False)
    monkeypatch.delenv("FPRV0_CONFIG_DIR", raising=False)
