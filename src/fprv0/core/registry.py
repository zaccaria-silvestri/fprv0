"""Registres des drivers : un driver réel et un simulateur par type d'instrument."""
from __future__ import annotations

from collections.abc import Callable

_REAL: dict[str, type] = {}
_SIM: dict[str, type] = {}


def register_instrument(name: str) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        _REAL[name] = cls
        cls.type_name = name
        return cls

    return decorator


def register_sim(name: str) -> Callable[[type], type]:
    def decorator(cls: type) -> type:
        _SIM[name] = cls
        return cls

    return decorator


def available_types() -> list[str]:
    return sorted(set(_REAL) | set(_SIM))


def create_instrument(type_name: str, transport: str, address: str, **kwargs):
    """Instancie le driver adapté au couple (type, transport)."""
    registry = _SIM if transport == "sim" else _REAL
    try:
        cls = registry[type_name]
    except KeyError:
        kind = "simulateur" if transport == "sim" else "driver"
        raise ValueError(
            f"Aucun {kind} enregistré pour le type '{type_name}' (types connus : {', '.join(available_types())})"
        ) from None
    return cls(address=address, **kwargs)
