"""Déclaration et validation des commandes exposées par les drivers.

Un driver déclare ses commandes avec ``@command`` ; l'API web n'accepte QUE ces commandes
(liste blanche) et valide les paramètres d'après les annotations de type de la méthode :

    class MonDriver(HTTPJSONInstrument):
        @command("Ouvrir la vanne", dangerous=False)
        def ouvrir(self, index: Annotated[int, Field(ge=0, le=7)]):
            ...

Attention : ``@command`` doit être posé sur la méthode de la classe qui la définit ; une
surcharge dans une sous-classe doit être redécorée pour rester exposée.
"""
from __future__ import annotations

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cache
from typing import Any, get_type_hints

from pydantic import TypeAdapter, ValidationError

_ATTR = "__fprv0_command__"


class CommandError(ValueError):
    """Commande inconnue ou paramètres invalides."""


def command(label: str | None = None, *, dangerous: bool = False) -> Callable:
    """Marque une méthode de driver comme commande appelable depuis l'API.

    ``dangerous=True`` impose une confirmation explicite (modale dans l'interface,
    ``"confirmed": true`` dans l'appel d'API).
    """

    def decorator(func: Callable) -> Callable:
        setattr(func, _ATTR, {"label": label or func.__name__.replace("_", " ").capitalize(), "dangerous": dangerous})
        return func

    return decorator


@dataclass(frozen=True)
class CommandSpec:
    name: str
    label: str
    dangerous: bool
    _adapters: dict[str, tuple[TypeAdapter, bool]] = field(repr=False)

    def validate(self, kwargs: Any) -> dict[str, Any]:
        if not isinstance(kwargs, dict):
            raise CommandError("'kwargs' doit être un objet JSON")
        unknown = sorted(set(kwargs) - set(self._adapters))
        if unknown:
            raise CommandError(f"Paramètres inconnus pour '{self.name}' : {', '.join(unknown)}")
        clean: dict[str, Any] = {}
        for pname, (adapter, required) in self._adapters.items():
            if pname not in kwargs:
                if required:
                    raise CommandError(f"Paramètre manquant pour '{self.name}' : {pname}")
                continue
            try:
                clean[pname] = adapter.validate_python(kwargs[pname])
            except ValidationError as exc:
                raise CommandError(f"Paramètre '{pname}' invalide : {exc.errors()[0]['msg']}") from exc
        return clean

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "dangerous": self.dangerous,
            "params": {
                pname: {"schema": adapter.json_schema(), "required": required}
                for pname, (adapter, required) in self._adapters.items()
            },
        }


@cache
def collect_commands(cls: type) -> dict[str, CommandSpec]:
    """Retourne les commandes déclarées sur ``cls`` (et ses parents), indexées par nom."""
    specs: dict[str, CommandSpec] = {}
    for name, func in inspect.getmembers(cls, inspect.isfunction):
        meta = getattr(func, _ATTR, None)
        if meta is None:
            continue
        hints = get_type_hints(func, include_extras=True)
        adapters: dict[str, tuple[TypeAdapter, bool]] = {}
        for pname, param in list(inspect.signature(func).parameters.items())[1:]:  # on saute self
            adapters[pname] = (TypeAdapter(hints.get(pname, Any)), param.default is inspect.Parameter.empty)
        specs[name] = CommandSpec(name=name, label=meta["label"], dangerous=meta["dangerous"], _adapters=adapters)
    return specs
