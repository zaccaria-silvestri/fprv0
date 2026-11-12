import pytest

from fprv0.config import Config, ConfigError, ensure_safe_binding, load_config

INSTRUMENTS = """
[a]
type = "eip_25b"
transport = "http"
address = "http://x/api"

[b]
type = "keithley_2000"
transport = "sim"
address = "http://y/api"
"""


def make_dir(tmp_path, app: str = "", instruments: str = INSTRUMENTS):
    if app:
        (tmp_path / "app.toml").write_text(app)
    (tmp_path / "instruments.toml").write_text(instruments)
    return tmp_path


def transports(config: Config) -> dict[str, str]:
    return {k: v.transport for k, v in config.instruments.items()}


def test_default_keeps_per_instrument_transport(tmp_path):
    config = load_config(make_dir(tmp_path, "[app]\nsimulation_mode = false\n"))
    assert transports(config) == {"a": "http", "b": "sim"}


def test_cli_sim_forces_every_instrument_to_sim(tmp_path):
    config = load_config(make_dir(tmp_path), sim=True)
    assert transports(config) == {"a": "sim", "b": "sim"}
    assert config.app.simulation_mode is True


def test_toml_simulation_mode_is_inherited(tmp_path):
    config = load_config(make_dir(tmp_path, "[app]\nsimulation_mode = true\n"))
    assert transports(config) == {"a": "sim", "b": "sim"}


def test_cli_no_sim_overrides_toml(tmp_path):
    config = load_config(make_dir(tmp_path, "[app]\nsimulation_mode = true\n"), sim=False)
    assert transports(config) == {"a": "http", "b": "sim"}  # retombe sur le transport de chaque instrument


def test_host_and_port_overrides(tmp_path):
    config = load_config(make_dir(tmp_path), host="0.0.0.0", port=8080)
    assert (config.app.web_host, config.app.web_port) == ("0.0.0.0", 8080)


def test_defaults_are_safe(tmp_path):
    config = load_config(make_dir(tmp_path))
    assert config.app.web_host == "127.0.0.1"
    ensure_safe_binding(config)


def test_network_binding_requires_token(tmp_path):
    config = load_config(make_dir(tmp_path), host="0.0.0.0")
    with pytest.raises(ConfigError, match="jeton"):
        ensure_safe_binding(config)


def test_network_binding_ok_with_token(tmp_path, monkeypatch):
    monkeypatch.setenv("FPRV0_API_TOKEN", "abc")
    config = load_config(make_dir(tmp_path), host="0.0.0.0")
    assert config.app.api_token == "abc"
    ensure_safe_binding(config)


@pytest.mark.parametrize("bad_id", ["../evil", "a b", "é"])
def test_instrument_id_is_validated(tmp_path, bad_id):
    toml = f'["{bad_id}"]\ntype = "eip_25b"\naddress = "http://x"\n'
    with pytest.raises(ConfigError, match="identifiant"):
        load_config(make_dir(tmp_path, instruments=toml))


def test_unknown_key_is_rejected(tmp_path):
    toml = '[a]\ntype = "eip_25b"\naddress = "http://x"\npoll_intervall_s = 1\n'
    with pytest.raises(ConfigError, match="poll_intervall_s"):
        load_config(make_dir(tmp_path, instruments=toml))


def test_invalid_transport_and_retry_bounds(tmp_path):
    with pytest.raises(ConfigError):
        load_config(make_dir(tmp_path, instruments='[a]\ntype="eip_25b"\naddress="x"\ntransport="carrier-pigeon"\n'))
    with pytest.raises(ConfigError, match="retry_max_s"):
        load_config(make_dir(tmp_path, instruments='[a]\ntype="eip_25b"\naddress="x"\nretry_min_s=5\nretry_max_s=1\n'))


def test_missing_config_dir(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "nope")
