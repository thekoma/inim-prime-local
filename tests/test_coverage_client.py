"""Coverage-completing tests for the vendored async client.

These exercise the write methods, the remaining getters, the HTTP/transport
error branches of ``_request``, the ``InimApiError`` unknown-status path, and
the ``_truthy`` fault-bit coercion helper.
"""

from __future__ import annotations

import json

import pytest

from custom_components.inim_prime.client import (
    ApiStatus,
    ArmMode,
    InimApiError,
    InimConnectionError,
    InimPrimeClient,
)
from custom_components.inim_prime.client.models import _truthy


class FakeResponse:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status = status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def text(self):
        return json.dumps(self._payload)


class FakeSession:
    """Session that records each request and replays a fixed success envelope."""

    def __init__(self, data=None, status: int = 200):
        # ``data`` is whatever the panel would return under the ``data`` key.
        self._payload = {"status": int(ApiStatus.SUCCESS), "data": data}
        self._http_status = status
        self.calls: list[dict] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": params, "timeout": timeout})
        return FakeResponse(self._payload, status=self._http_status)


def _client(session, **kw):
    return InimPrimeClient("1.2.3.4", 8080, "KEY", session, **kw)


# ---------------------------------------------------------------------------
# _request transport / status error branches
# ---------------------------------------------------------------------------
async def test_request_http_non_200_raises_connection_error():
    """A non-200 HTTP status is surfaced as InimConnectionError."""
    session = FakeSession(status=503)
    with pytest.raises(InimConnectionError) as exc:
        await _client(session).ping()
    assert "503" in str(exc.value)


async def test_api_error_unknown_status_labels_unknown():
    """An int status outside ApiStatus is kept raw and labeled UNKNOWN."""
    err = InimApiError(999)
    assert err.status == 999
    assert "UNKNOWN" in str(err)
    assert "999" in str(err)


# ---------------------------------------------------------------------------
# Remaining read methods
# ---------------------------------------------------------------------------
async def test_ping_returns_true():
    session = FakeSession(data="pong")
    assert await _client(session).ping() is True
    assert session.calls[0]["params"]["cmd"] == "ping"


async def test_get_areas_get_scenarios_get_outputs():
    areas = await _client(
        FakeSession(data={"part": [{"id": "1", "lb": "Home", "am": "4", "st": "1", "mm": "0"}]})
    ).get_areas()
    assert areas[0].label == "Home"

    scenarios = await _client(
        FakeSession(data={"sce": [{"id": "1", "lb": "Away", "st": "0"}]})
    ).get_scenarios()
    assert scenarios[0].label == "Away"

    outputs = await _client(
        FakeSession(data={"cmd": [{"id": "1", "lb": "Siren", "tl": "1", "st": "0", "t": "0"}]})
    ).get_outputs()
    assert outputs[0].label == "Siren"


async def test_get_faults():
    fault = await _client(FakeSession(data={"vcc": "13,7", "fau": "0"})).get_faults()
    assert fault.has_faults is False


# ---------------------------------------------------------------------------
# Write methods — assert the exact cmd/params sent
# ---------------------------------------------------------------------------
async def test_arm_area_sends_set_partitions():
    session = FakeSession(data=None)
    await _client(session).arm_area(2, ArmMode.PARTIAL)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_partitions_mode"
    assert p["p1"] == "2"
    assert p["p2"] == str(int(ArmMode.PARTIAL))


async def test_disarm_area_sends_disarm_mode():
    session = FakeSession(data=None)
    await _client(session).disarm_area(3)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_partitions_mode"
    assert p["p1"] == "3"
    assert p["p2"] == str(int(ArmMode.DISARM))


async def test_apply_scenario_sends_set_scenarios():
    session = FakeSession(data=None)
    await _client(session).apply_scenario(7)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_scenarios_mode"
    assert p["p1"] == "7"


async def test_set_output_sends_value():
    session = FakeSession(data=None)
    await _client(session).set_output(4, 1)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_outputs_mode"
    assert p["p1"] == "4"
    assert p["p2"] == "1"


@pytest.mark.parametrize(("excluded", "expected"), [(True, "1"), (False, "0")])
async def test_set_zone_excluded_maps_bool(excluded, expected):
    session = FakeSession(data=None)
    await _client(session).set_zone_excluded(5, excluded)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_zones_mode"
    assert p["p1"] == "5"
    assert p["p2"] == expected


async def test_clear_alarm_memory_sends_clear_mode():
    session = FakeSession(data=None)
    await _client(session).clear_alarm_memory(1)
    p = session.calls[0]["params"]
    assert p["cmd"] == "set_partitions_mode"
    assert p["p2"] == str(int(ArmMode.CLEAR_MEMORY))


# ---------------------------------------------------------------------------
# _truthy fault-bit coercion
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (3.0, True),
        (0.0, False),
        ("yes", True),
        ("y", True),
        ("on", True),
        ("true", True),
        ("1", True),
        ("", False),
        ("off", False),
        ("no", False),
        ("false", False),
        ("0", False),
        ("2.5", True),
        ("0.0", False),
        ("garbage", False),
        (None, False),
        (object(), False),
    ],
)
def test_truthy_coercion(value, expected):
    assert _truthy(value) is expected
