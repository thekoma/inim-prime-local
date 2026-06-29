import json

import pytest

from custom_components.inim_prime.client import (
    ApiStatus,
    InimApiError,
    InimConnectionError,
    InimPrimeClient,
)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    def __init__(self, payload: dict):
        self._payload = payload
        self.last_url = None
        self.last_params = None

    def get(self, url, params=None, timeout=None):
        self.last_url = url
        self.last_params = params
        return FakeResponse(self._payload)


def _client(session):
    return InimPrimeClient("1.2.3.4", 8080, "KEY", session)


async def test_version_parsed(load_fixture):
    session = FakeSession(load_fixture("version"))
    v = await _client(session).version()
    assert v.version == "1.0.1"
    assert "/cgi-bin/api.cgi" in session.last_url
    assert session.last_params["apikey"] == "KEY"
    assert session.last_params["cmd"] == "version"


async def test_get_zones_returns_models(load_fixture):
    session = FakeSession(load_fixture("zones"))
    zones = await _client(session).get_zones()
    assert len(zones) == 3
    assert zones[0].label == "Fin.Bagno PT"


async def test_nonzero_status_raises(load_fixture):
    session = FakeSession(load_fixture("gsm_not_implemented"))
    with pytest.raises(InimApiError) as exc:
        await _client(session)._request("get_gsm_status")
    assert exc.value.status == ApiStatus.NOT_IMPLEMENTED


async def test_get_area_open_zones(load_fixture):
    from custom_components.inim_prime.client import ArmMode

    session = FakeSession(load_fixture("partitions_nrz"))
    zones = await _client(session).get_area_open_zones(0, ArmMode.TOTAL)
    assert len(zones) == 1
    assert zones[0].label == "Fin.Bagno PT"
    assert session.last_params["cmd"] == "get_partitions_nrz"
    assert session.last_params["p1"] == "0"
    assert session.last_params["p2"] == "1"


async def test_get_area_open_zones_empty(load_fixture):
    session = FakeSession(load_fixture("partitions_nrz_empty"))
    zones = await _client(session).get_area_open_zones(0)
    assert zones == []


async def test_get_scenario_open_zones(load_fixture):
    session = FakeSession(load_fixture("scenarios_nrz"))
    zones = await _client(session).get_scenario_open_zones(0)
    assert zones[0].label == "Fin.Bagno PT"
    assert session.last_params["cmd"] == "get_scenarios_nrz"
    assert session.last_params["p1"] == "0"


async def test_get_timers_returns_models(load_fixture):
    session = FakeSession(load_fixture("timers"))
    timers = await _client(session).get_timers()
    assert session.last_params["cmd"] == "get_timers_status"
    assert len(timers) == 2
    assert next(t for t in timers if t.id == 0).active is True
    assert next(t for t in timers if t.id == 10).active is False


async def test_get_api_stats_returns_model(load_fixture):
    session = FakeSession(load_fixture("status_api"))
    stats = await _client(session).get_api_stats()
    assert session.last_params["cmd"] == "get_status_api"
    assert stats.connections == 188
    assert stats.last_ip == "192.168.85.25"


async def test_get_authorized_ips_returns_model(load_fixture):
    session = FakeSession(load_fixture("ip_auth"))
    acl = await _client(session).get_authorized_ips()
    assert session.last_params["cmd"] == "get_ip_autorizzati"
    assert acl.only_enabled is False
    assert acl.ips == ["192.168.85.10"]


async def test_get_authorized_macs_returns_model(load_fixture):
    session = FakeSession(load_fixture("mac_auth"))
    acl = await _client(session).get_authorized_macs()
    assert session.last_params["cmd"] == "get_mac_autorizzati"
    assert acl.only_enabled is True
    assert acl.macs == ["AA-BB-CC-DD-EE-FF"]


def test_api_error_labels_new_statuses():
    assert "CODE_NOT_ALLOWED" in str(InimApiError(8))
    assert "ZONES_NOT_READY" in str(InimApiError(11))
    assert InimApiError(8).status == ApiStatus.CODE_NOT_ALLOWED


class TimeoutSession:
    def get(self, url, params=None, timeout=None):
        class _CM:
            async def __aenter__(self_inner):
                raise TimeoutError

            async def __aexit__(self_inner, *exc):
                return False

        return _CM()


async def test_timeout_wrapped_as_connection_error():
    client = InimPrimeClient("1.2.3.4", 8080, "KEY", TimeoutSession())
    with pytest.raises(InimConnectionError):
        await client.version()
