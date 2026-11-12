import time
from typing import Annotated

from pydantic import Field

from fprv0.core.commands import command
from fprv0.core.registry import register_instrument, register_sim
from fprv0.instruments.base import HTTPJSONInstrument
from fprv0.instruments.models import ValveStatus

ValveIndex = Annotated[int, Field(ge=0, le=7)]
ValveState = Annotated[int, Field(ge=0, le=1)]  # 0 = fermée, 1 = ouverte


@register_instrument("swagelok_dp")
class SwagelokDP(HTTPJSONInstrument):
    """Actionneurs de vannes pneumatiques SwagelokDP / EQB-Pascal (ESP32).

    ``address`` pointe sur ``.../api/status`` ; les commandes utilisent ``.../api/toggle``,
    ``.../api/toggle-all`` et ``.../api/reboot``.
    """

    model = ValveStatus
    ui = {
        "kind": "valves",
        "title": "Vannes pneumatiques SwagelokDP",
        "icon": "🚰",
        "toggle_command": "set_vanne_state",
        "actions": [
            {"label": "Tout ouvrir", "command": "toggle_all", "kwargs": {"state": 1}, "style": "open"},
            {"label": "Tout fermer", "command": "toggle_all", "kwargs": {"state": 0}, "style": "closed"},
            {"label": "Redémarrer l'ESP32", "command": "reboot", "kwargs": {}, "style": "neutral"},
        ],
    }

    @property
    def _base_url(self) -> str:
        return self.address.removesuffix("/status").rstrip("/")

    @classmethod
    def history_values(cls, data: dict) -> dict[str, float | None]:
        """Une série par vanne raccordée ; la moyenne sur la période = fraction de temps ouverte."""
        out: dict[str, float | None] = {}
        for i, (state, enabled) in enumerate(zip(data.get("vannes", []), data.get("enabled", []), strict=False)):
            if enabled == 1 and state in (0, 1):
                out[f"vanne_{i + 1}"] = float(state)
        return out

    # --- commandes exposées (liste blanche) -------------------------------------------------

    @command("Ouvrir / fermer une vanne")
    def set_vanne_state(self, index: ValveIndex, state: ValveState):
        return self._valve_toggle(index, state)

    @command("Ouvrir / fermer toutes les vannes", dangerous=True)
    def toggle_all(self, state: ValveState):
        return self._toggle_all(state)

    @command("Redémarrer le contrôleur", dangerous=True)
    def reboot(self):
        return self._reboot()

    # --- accès appareil (surchargés par le simulateur) --------------------------------------

    def _valve_toggle(self, index: int, state: int):
        return self._get(f"{self._base_url}/toggle", {"id": index + 1, "state": state})

    def _toggle_all(self, state: int):
        return self._get(f"{self._base_url}/toggle-all", {"state": state})

    def _reboot(self):
        return self._get(f"{self._base_url}/reboot")


@register_sim("swagelok_dp")
class SwagelokDPSim(SwagelokDP):
    def __init__(self, address: str, timeout: float = 3.0) -> None:
        super().__init__(address, timeout)
        self.start_time = time.time()
        self.vannes = [0, 0, 0, 0, -1, -1, -1, -1]
        self.enabled = [1, 1, 1, 1, 0, 0, 0, 0]
        self.names = [
            "Vanne 1 - Sortie FP",
            "Vanne 2 - FPG",
            "Vanne 3 - Pace1000",
            "Vanne 4 - Vide",
            "Vanne 5",
            "Vanne 6",
            "Vanne 7",
            "Vanne 8",
        ]

    def connect(self) -> None:
        pass

    def disconnect(self) -> None:
        pass

    def fetch_raw(self) -> dict:
        return {
            "uptime": int(time.time() - self.start_time),
            "vannes": list(self.vannes),
            "enabled": list(self.enabled),
            "names": list(self.names),
        }

    def _valve_toggle(self, index: int, state: int):
        if self.enabled[index] == 1:
            self.vannes[index] = state
        return {"ok": True}

    def _toggle_all(self, state: int):
        for i, enabled in enumerate(self.enabled):
            if enabled == 1:
                self.vannes[i] = state
        return {"ok": True}

    def _reboot(self):
        self.start_time = time.time()
        return {"ok": True}
