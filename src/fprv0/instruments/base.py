"""Classes de base des drivers.

Pour ajouter un instrument HTTP/JSON il suffit de :
  1. décrire la réponse de l'appareil dans ``models.py`` (modèle pydantic) ;
  2. sous-classer ``HTTPJSONInstrument`` en renseignant ``model`` et ``ui`` ;
  3. déclarer les commandes avec ``@command`` ;
  4. écrire un simulateur qui ne redéfinit que ``connect``/``disconnect``/``fetch_raw`` (et les
     méthodes ``_xxx`` appelées par les commandes), puis enregistrer les deux classes.
L'interface web se construit toute seule à partir de ``ui`` et de la liste des commandes.
"""
from __future__ import annotations

import math
import time
from abc import ABC, abstractmethod
from typing import Any, ClassVar

from pydantic import BaseModel

from fprv0.core.commands import CommandError, CommandSpec, collect_commands
from fprv0.transports.http_transport import HTTPJSONTransport

# Bloc de diagnostic commun à plusieurs appareils (format = comment le navigateur l'affiche).
STANDARD_DIAGNOSTICS: list[dict[str, Any]] = [
    {"key": "gpib_ok", "label": "GPIB", "format": "ok"},
    {"key": "eth_ok", "label": "Ethernet", "format": "ok"},
    {"key": "uptime", "label": "En marche depuis", "format": "seconds"},
    {"key": "heap", "label": "Mémoire libre", "format": "bytes"},
    {"key": "reset", "label": "Dernier reset", "format": "text"},
    {"key": "fault", "label": "Défaut", "format": "fault", "msg_key": "faultMsg"},
]


def _finite_or_none(value: Any) -> Any:
    """JSON n'accepte pas NaN/Infinity : on les remplace par None (cellule vide en CSV)."""
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {k: _finite_or_none(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_finite_or_none(v) for v in value]
    return value


class BaseInstrument(ABC):
    type_name: ClassVar[str] = ""
    model: ClassVar[type[BaseModel]]
    #: Description déclarative de la carte affichée dans l'interface web.
    ui: ClassVar[dict[str, Any]] = {}

    def __init__(self, address: str, timeout: float = 3.0) -> None:
        self.address = address
        self.timeout = timeout

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def fetch_raw(self) -> dict:
        """Lit la réponse brute (dict JSON) de l'appareil."""

    def poll(self) -> dict:
        """Mesure validée, nettoyée et horodatée (heure de l'hôte)."""
        reading = self.model.model_validate(self.fetch_raw())
        data = _finite_or_none(reading.model_dump())
        data["timestamp"] = time.time()
        return data

    @classmethod
    def history_values(cls, data: dict) -> dict[str, float | None]:
        """Valeurs numériques à historiser en base (moyennées par période).

        Par défaut : les mesures déclarées dans ``ui["readings"]``. Un driver peut surcharger.
        """
        return {r["key"]: data.get(r["key"]) for r in cls.ui.get("readings", [])}

    @classmethod
    def commands(cls) -> dict[str, CommandSpec]:
        return collect_commands(cls)

    def execute(self, name: str, kwargs: dict[str, Any] | None = None) -> Any:
        """Exécute une commande déclarée avec ``@command`` (paramètres revalidés ici aussi)."""
        spec = self.commands().get(name)
        if spec is None:
            raise CommandError(f"Commande inconnue : '{name}'")
        return getattr(self, name)(**spec.validate(kwargs or {}))


class HTTPJSONInstrument(BaseInstrument):
    """Appareil interrogé par GET HTTP renvoyant du JSON."""

    def __init__(self, address: str, timeout: float = 3.0) -> None:
        super().__init__(address, timeout)
        self.transport = HTTPJSONTransport(url=address, timeout=timeout)

    def connect(self) -> None:
        self.transport.open()
        try:
            self.poll()  # sonde réelle : joignable ET réponse conforme au modèle
        except Exception:
            self.transport.close()
            raise

    def disconnect(self) -> None:
        self.transport.close()

    def fetch_raw(self) -> dict:
        return self.transport.get_json()

    def _get(self, url: str, params: dict | None = None) -> dict:
        return self.transport.get_url(url, params)
