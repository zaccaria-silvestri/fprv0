import random
import time

from fprv0.core.registry import register_instrument, register_sim
from fprv0.instruments.base import STANDARD_DIAGNOSTICS, HTTPJSONInstrument
from fprv0.instruments.models import EIPReading


@register_instrument("eip_25b")
class EIP25B(HTTPJSONInstrument):
    """Compteur de fréquence EIP 25B (API HTTP/JSON)."""

    model = EIPReading
    ui = {
        "kind": "readings",
        "title": "Fréquencemètre EIP 25B",
        "icon": "🌐",
        "readings": [{"key": "freq", "label": "Fréquence", "format": "frequency", "color": "#2b8a3e"}],
        "plot": {"key": "freq", "label": "Fréquence (Hz)", "color": "#2b8a3e"},
        "diagnostics": STANDARD_DIAGNOSTICS,
    }


@register_sim("eip_25b")
class EIP25BSim(EIP25B):
    def __init__(self, address: str, timeout: float = 3.0) -> None:
        super().__init__(address, timeout)
        self.base_freq = 52310121.00
        self.start_time = time.time()

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def fetch_raw(self) -> dict:
        freq = self.base_freq + random.gauss(0, 0.5)
        return {
            "rawFreq": freq,
            "freq": freq,
            "offset": 0.00,
            "gpib_ok": 1,
            "eth_ok": 1,
            "uptime": int(time.time() - self.start_time),
            "heap": 352900 + random.randint(-100, 100),
            "reset": "Plantage (Watchdog Timer)",
            "fault": 0,
            "faultMsg": "",
            "age": 10,
            "stale": 0,
            "gpibRec": 0,
            "ethRec": 0,
        }
