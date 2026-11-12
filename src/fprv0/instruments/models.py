"""Modèles de validation des réponses des appareils.

Les champs de diagnostic sont optionnels et les champs inconnus sont conservés
(``extra="allow"``) : un firmware qui ajoute une clé ne casse rien. En revanche, une mesure
principale absente ou de mauvais type (ex. ``freq`` manquant ou textuel) lève une erreur de
validation explicite au lieu de produire un ``KeyError`` plus loin dans l'application.
Resserrez ces modèles quand le format exact des firmwares est figé.
"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

Number = int | float


class _Reading(BaseModel):
    model_config = ConfigDict(extra="allow")

    gpib_ok: int | None = None
    eth_ok: int | None = None
    uptime: Number | None = None
    heap: Number | None = None
    reset: str | None = None
    fault: int | None = None
    faultMsg: str | None = None


class EIPReading(_Reading):
    freq: float
    rawFreq: float | None = None
    offset: Number | None = None
    age: Number | None = None
    stale: int | None = None
    gpibRec: Number | None = None
    ethRec: Number | None = None


class KeithleyReading(_Reading):
    temperature: float
    resistance: float
    rawTemp: float | None = None
    offset: Number | None = None


class ValveStatus(BaseModel):
    model_config = ConfigDict(extra="allow")

    uptime: Number | None = None
    vannes: list[int]
    enabled: list[int] = Field(default_factory=list)
    names: list[str] = Field(default_factory=list)
