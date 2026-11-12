import random
import time

from fprv0.core.registry import register_instrument, register_sim
from fprv0.instruments.base import STANDARD_DIAGNOSTICS, HTTPJSONInstrument
from fprv0.instruments.models import KeithleyReading


@register_instrument("keithley_2000")
class Keithley2000(HTTPJSONInstrument):
    """Multimètre / thermomètre Keithley 2000/2001 (API HTTP/JSON)."""

    model = KeithleyReading
    ui = {
        "kind": "readings",
        "title": "Keithley 2000/2001",
        "icon": "🌡️",
        "readings": [
            {"key": "temperature", "label": "Température", "format": "fixed", "decimals": 4, "unit": "°C", "color": "#e03131"},
            {"key": "resistance", "label": "Résistance", "format": "fixed", "decimals": 4, "unit": "Ω", "color": "#1c7ed6"},
        ],
        "plot": {"key": "temperature", "label": "Température (°C)", "color": "#e03131"},
        "diagnostics": STANDARD_DIAGNOSTICS,
    }


@register_sim("keithley_2000")
class Keithley2000Sim(Keithley2000):
    def __init__(self, address: str, timeout: float = 3.0) -> None:
        super().__init__(address, timeout)
        self.start_time = time.time()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def fetch_raw(self) -> dict:
        temp = 21.0229 + random.gauss(0, 0.005)
        res = 108.1908 + (temp - 21.0229) * 0.385 + random.gauss(0, 0.001)
        return {
            "resistance": round(res, 4),
            "temperature": round(temp, 4),
            "rawTemp": round(temp, 4),
            "offset": 0.000,
            "gpib_ok": 1,
            "eth_ok": 1,
            "uptime": int(time.time() - self.start_time),
            "heap": 354600 + random.randint(-50, 50),
            "reset": "Coupure secteur / Power-On",
            "fault": 0,
            "faultMsg": "",
        }
