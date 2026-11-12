"""Chargement et validation de la configuration.

Priorité pour décider si un instrument est simulé :

1. option CLI explicite ``--sim`` / ``--no-sim``
2. ``simulation_mode`` dans ``app.toml``
3. champ ``transport`` de chaque instrument (``"http"`` ou ``"sim"``)

Quand la simulation globale est active, tous les instruments passent en ``transport = "sim"``.
Après ``load_config``, ``InstrumentConfig.transport`` est donc toujours le transport *effectif*.
"""
from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
ENV_TOKEN = "FPRV0_API_TOKEN"
ENV_CONFIG_DIR = "FPRV0_CONFIG_DIR"


class ConfigError(Exception):
    """Configuration invalide ou introuvable (message destiné à l'utilisateur)."""


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = "fprv0"
    simulation_mode: bool = False
    web_host: str = "127.0.0.1"
    web_port: int = Field(5000, ge=1, le=65535)
    api_token: str | None = None
    data_dir: Path = Path("data")
    save_csv: bool = True
    history_points: int = Field(3600, ge=0)
    # Base de données historique (moyennes par période, journal, audit des commandes)
    history_db: bool = True
    db_file: str = "history.db"            # relatif à data_dir
    db_period_s: int = Field(60, ge=1)     # une moyenne enregistrée par instrument et par période
    db_retention_days: int = Field(90, ge=1)


class InstrumentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    address: str
    transport: Literal["http", "sim"] = "http"
    label: str | None = None
    poll_interval_s: float = Field(1.0, gt=0)
    enabled: bool = True
    timeout_s: float = Field(3.0, gt=0)
    retry_min_s: float = Field(1.0, gt=0)
    retry_max_s: float = Field(30.0, gt=0)
    max_failures: int = Field(3, ge=1)

    @model_validator(mode="after")
    def _check_retry_bounds(self) -> InstrumentConfig:
        if self.retry_max_s < self.retry_min_s:
            raise ValueError("retry_max_s doit être >= retry_min_s")
        return self


class Config(BaseModel):
    app: AppConfig
    instruments: dict[str, InstrumentConfig]
    config_dir: Path | None = None


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def ensure_safe_binding(config: Config) -> None:
    """Refuse d'exposer l'API de commande au réseau sans jeton."""
    app = config.app
    if not is_loopback(app.web_host) and not app.api_token:
        raise ConfigError(
            f"web_host = '{app.web_host}' expose l'interface au réseau, mais aucun jeton d'API n'est défini. "
            f"Définissez {ENV_TOKEN} (ou api_token dans app.toml), ou écoutez sur 127.0.0.1."
        )


def _find_config_dir(explicit: str | Path | None) -> Path:
    if explicit:
        path = Path(explicit)
        if not path.is_dir():
            raise ConfigError(f"Dossier de configuration introuvable : {path}")
        return path

    candidates = []
    if os.environ.get(ENV_CONFIG_DIR):
        candidates.append(Path(os.environ[ENV_CONFIG_DIR]))
    candidates.append(Path.cwd() / "config")
    candidates.append(Path(__file__).resolve().parents[2] / "config")  # checkout git / pip install -e

    for path in candidates:
        if (path / "instruments.toml").is_file():
            return path
    searched = ", ".join(str(p) for p in candidates)
    raise ConfigError(f"Aucun instruments.toml trouvé (cherché dans : {searched}). Utilisez --config-dir.")


def _read_toml(path: Path) -> dict:
    try:
        with open(path, "rb") as fh:
            return tomllib.load(fh)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} : TOML invalide ({exc})") from exc


def _format_validation(label: str, exc: ValidationError) -> str:
    lines = [f"{label} : configuration invalide"]
    for err in exc.errors():
        loc = ".".join(str(part) for part in err["loc"])
        lines.append(f"  - {loc}: {err['msg']}")
    return "\n".join(lines)


def load_config(
    config_dir: str | Path | None = None,
    *,
    sim: bool | None = None,
    host: str | None = None,
    port: int | None = None,
) -> Config:
    directory = _find_config_dir(config_dir)

    app_path = directory / "app.toml"
    raw_app = _read_toml(app_path).get("app", {}) if app_path.is_file() else {}
    try:
        app = AppConfig(**raw_app)
    except ValidationError as exc:
        raise ConfigError(_format_validation(str(app_path), exc)) from exc

    # Surcharges explicites (CLI / environnement).
    if sim is not None:
        app.simulation_mode = sim
    if host is not None:
        app.web_host = host
    if port is not None:
        app.web_port = port
    if os.environ.get(ENV_TOKEN):
        app.api_token = os.environ[ENV_TOKEN]

    inst_path = directory / "instruments.toml"
    if not inst_path.is_file():
        raise ConfigError(f"Fichier introuvable : {inst_path}")
    raw_instruments = _read_toml(inst_path)
    if not raw_instruments:
        raise ConfigError(f"{inst_path} : aucun instrument défini")

    instruments: dict[str, InstrumentConfig] = {}
    for inst_id, raw in raw_instruments.items():
        if not ID_PATTERN.match(inst_id):
            raise ConfigError(
                f"{inst_path} : identifiant d'instrument invalide '{inst_id}' "
                "(lettres, chiffres, '_' et '-' uniquement)"
            )
        if not isinstance(raw, dict):
            raise ConfigError(f"{inst_path} : '{inst_id}' doit être une table [{inst_id}]")
        try:
            cfg = InstrumentConfig(**raw)
        except ValidationError as exc:
            raise ConfigError(_format_validation(f"{inst_path} [{inst_id}]", exc)) from exc
        if app.simulation_mode:
            cfg = cfg.model_copy(update={"transport": "sim"})
        instruments[inst_id] = cfg

    return Config(app=app, instruments=instruments, config_dir=directory)
