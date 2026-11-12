import httpx
import pytest

from fprv0.core.commands import CommandError, command
from fprv0.core.registry import create_instrument
from fprv0.instruments.eip_25b import EIP25B
from fprv0.instruments.swagelok_dp import SwagelokDP, SwagelokDPSim


def make_http_driver(cls, address, handler):
    driver = cls(address=address)
    driver.transport.client = httpx.Client(transport=httpx.MockTransport(handler))
    return driver


# --------------------------------------------------------------------------- commandes

def test_only_decorated_methods_are_commands():
    names = set(SwagelokDP.commands())
    assert names == {"set_vanne_state", "toggle_all", "reboot"}
    assert not {"connect", "poll", "disconnect", "execute", "fetch_raw", "_reboot"} & names


def test_dangerous_flags():
    specs = SwagelokDP.commands()
    assert specs["toggle_all"].dangerous and specs["reboot"].dangerous
    assert not specs["set_vanne_state"].dangerous


def test_commands_survive_in_simulator_subclass():
    assert set(SwagelokDPSim.commands()) == set(SwagelokDP.commands())


def test_parameter_validation():
    spec = SwagelokDP.commands()["set_vanne_state"]
    assert spec.validate({"index": 2, "state": 1}) == {"index": 2, "state": 1}
    with pytest.raises(CommandError, match="index"):
        spec.validate({"index": 99, "state": 1})
    with pytest.raises(CommandError, match="state"):
        spec.validate({"index": 1, "state": 2})
    with pytest.raises(CommandError, match="manquant"):
        spec.validate({"index": 1})
    with pytest.raises(CommandError, match="inconnus"):
        spec.validate({"index": 1, "state": 1, "evil": True})
    with pytest.raises(CommandError):
        spec.validate("not a dict")


def test_spec_exposes_json_schema_to_the_ui():
    params = SwagelokDP.commands()["set_vanne_state"].to_dict()["params"]
    assert params["index"]["schema"]["maximum"] == 7
    assert params["index"]["required"] is True


def test_custom_command_decorator_defaults():
    class Dummy:
        @command()
        def do_it_now(self):
            ...

    from fprv0.core.commands import collect_commands

    spec = collect_commands(Dummy)["do_it_now"]
    assert spec.label == "Do it now" and spec.dangerous is False


# --------------------------------------------------------------------------- simulateurs

def test_swagelok_sim_executes_commands():
    sim = create_instrument("swagelok_dp", "sim", "http://x/api/status")
    sim.execute("set_vanne_state", {"index": 0, "state": 1})
    assert sim.poll()["vannes"][:2] == [1, 0]
    sim.execute("toggle_all", {"state": 1})
    assert sim.poll()["vannes"] == [1, 1, 1, 1, -1, -1, -1, -1]  # les vannes non raccordées ne bougent pas
    with pytest.raises(CommandError):
        sim.execute("poll")
    with pytest.raises(CommandError):
        sim.execute("set_vanne_state", {"index": 0})


@pytest.mark.parametrize("type_name", ["eip_25b", "keithley_2000", "swagelok_dp"])
def test_all_simulators_produce_valid_timestamped_data(type_name):
    sim = create_instrument(type_name, "sim", "http://x/api")
    sim.connect()
    data = sim.poll()
    assert isinstance(data["timestamp"], float)
    sim.disconnect()


def test_eip_sim_values():
    data = create_instrument("eip_25b", "sim", "http://x").poll()
    assert abs(data["freq"] - 52310121.0) < 10
    assert data["gpib_ok"] == 1 and data["eth_ok"] == 1 and "uptime" in data and "rawFreq" in data


def test_unknown_type_is_reported_with_known_types():
    with pytest.raises(ValueError, match="eip_25b"):
        create_instrument("nope", "http", "x")


# --------------------------------------------------------------------------- driver HTTP réel (transport simulé)

def test_http_driver_validates_and_stamps():
    driver = make_http_driver(EIP25B, "http://dev/api", lambda req: httpx.Response(200, json={"freq": 1e6, "future_field": 7}))
    driver.connect()
    data = driver.poll()
    assert data["freq"] == 1e6 and data["future_field"] == 7 and "timestamp" in data  # champ inconnu conservé


def test_http_driver_rejects_malformed_reply_at_connect():
    driver = make_http_driver(EIP25B, "http://dev/api", lambda req: httpx.Response(200, json={"oops": 1}))
    with pytest.raises(Exception, match="freq"):
        driver.connect()
    assert driver.transport.client is None  # connexion refermée proprement


def test_http_driver_http_error_propagates():
    driver = make_http_driver(EIP25B, "http://dev/api", lambda req: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        driver.connect()


def test_nan_is_replaced_by_none():
    driver = make_http_driver(EIP25B, "http://dev/api", lambda req: httpx.Response(200, content=b'{"freq": 5, "offset": NaN}'))
    data = driver.poll()
    assert data["offset"] is None


def test_swagelok_real_commands_hit_the_right_endpoints():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, dict(request.url.params)))
        return httpx.Response(200, json={"ok": True})

    driver = make_http_driver(SwagelokDP, "http://esp/api/status", handler)
    driver.execute("set_vanne_state", {"index": 2, "state": 1})
    driver.execute("toggle_all", {"state": 0})
    driver.execute("reboot")
    assert seen == [
        ("/api/toggle", {"id": "3", "state": "1"}),  # index 0-based -> id 1-based
        ("/api/toggle-all", {"state": "0"}),
        ("/api/reboot", {}),
    ]


def test_command_tolerates_non_json_reply():
    driver = make_http_driver(SwagelokDP, "http://esp/api/status", lambda req: httpx.Response(200, text="OK"))
    assert driver.execute("reboot") == {"text": "OK"}
