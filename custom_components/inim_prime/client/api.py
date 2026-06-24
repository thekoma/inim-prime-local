"""Async HTTP client for the INIM PrimeX local API."""
from __future__ import annotations

import json
from typing import Any

import aiohttp

from .const import API_PATH, ApiStatus, ArmMode, Command
from .models import (
    ApiStats,
    Area,
    Fault,
    IpAcl,
    MacAcl,
    OpenZone,
    Output,
    Scenario,
    Timer,
    Version,
    Zone,
)


class InimError(Exception):
    """Base error."""


class InimApiError(InimError):
    def __init__(self, status: ApiStatus | int):
        self.status: ApiStatus | int
        try:
            self.status = ApiStatus(status)
            name = self.status.name
        except ValueError:
            self.status = status
            name = "UNKNOWN"
        super().__init__(f"panel returned status {int(status)} ({name})")


class InimConnectionError(InimError):
    """Transport/connection failure."""


class InimPrimeClient:
    def __init__(self, host: str, port: int, apikey: str,
                 session: aiohttp.ClientSession,
                 *, use_https: bool = False, timeout: int = 10,
                 connect_timeout: float | None = None):
        scheme = "https" if use_https else "http"
        self._base = f"{scheme}://{host}:{port}{API_PATH}"
        self._apikey = apikey
        self._session = session
        self._timeout = timeout
        # A separate (shorter) socket-connect ceiling lets an unreachable panel
        # fail fast instead of consuming the whole total budget on the TCP
        # handshake. ``None`` keeps the previous behavior (total only).
        self._connect_timeout = connect_timeout

    async def _request(self, cmd: str, **params: object) -> Any:
        query = {"apikey": self._apikey, "cmd": str(cmd), **{k: str(v) for k, v in params.items()}}
        client_timeout = aiohttp.ClientTimeout(
            total=self._timeout, sock_connect=self._connect_timeout
        )
        try:
            async with self._session.get(self._base, params=query, timeout=client_timeout) as resp:
                if resp.status != 200:
                    raise InimConnectionError(f"HTTP {resp.status}")
                body = await resp.text()
        except (TimeoutError, aiohttp.ClientError) as err:
            raise InimConnectionError(str(err)) from err
        envelope = json.loads(body)
        status = envelope.get("status")
        if status != ApiStatus.SUCCESS:
            raise InimApiError(status)
        return envelope.get("data")

    async def version(self) -> Version:
        return Version.from_raw(await self._request(Command.VERSION))

    async def ping(self) -> bool:
        return bool(await self._request(Command.PING) == "pong")

    async def get_zones(self) -> list[Zone]:
        data = await self._request(Command.GET_ZONES)
        return [Zone.from_raw(z) for z in data["zone"]]

    async def get_areas(self) -> list[Area]:
        data = await self._request(Command.GET_PARTITIONS)
        return [Area.from_raw(a) for a in data["part"]]

    async def get_scenarios(self) -> list[Scenario]:
        data = await self._request(Command.GET_SCENARIOS)
        return [Scenario.from_raw(s) for s in data["sce"]]

    async def get_outputs(self) -> list[Output]:
        data = await self._request(Command.GET_OUTPUTS)
        return [Output.from_raw(o) for o in data["cmd"]]

    async def get_faults(self) -> Fault:
        return Fault.from_raw(await self._request(Command.GET_FAULTS))

    async def get_area_open_zones(self, area_id: int, mode: ArmMode = ArmMode.TOTAL) -> list[OpenZone]:
        data = await self._request(Command.GET_PARTITIONS_NRZ, p1=area_id, p2=int(mode))
        return [OpenZone.from_raw(z) for z in data["nrz"]]

    async def get_scenario_open_zones(self, scenario_id: int) -> list[OpenZone]:
        data = await self._request(Command.GET_SCENARIOS_NRZ, p1=scenario_id)
        return [OpenZone.from_raw(z) for z in data["nrz"]]

    async def get_timers(self) -> list[Timer]:
        data = await self._request(Command.GET_TIMERS_STATUS)
        return [Timer.from_raw(t) for t in data["tmr"]]

    async def get_api_stats(self) -> ApiStats:
        data = await self._request(Command.GET_STATUS_API)
        return ApiStats.from_raw(data["status"][0])

    async def get_authorized_ips(self) -> IpAcl:
        return IpAcl.from_raw(await self._request(Command.GET_IP_AUTH))

    async def get_authorized_macs(self) -> MacAcl:
        return MacAcl.from_raw(await self._request(Command.GET_MAC_AUTH))

    # --- write methods (NOT used by probe.py) ---
    async def arm_area(self, area_id: int, mode: ArmMode = ArmMode.TOTAL) -> object:
        return await self._request(Command.SET_PARTITIONS, p1=area_id, p2=int(mode))

    async def disarm_area(self, area_id: int) -> object:
        return await self._request(Command.SET_PARTITIONS, p1=area_id, p2=int(ArmMode.DISARM))

    async def apply_scenario(self, scenario_id: int) -> object:
        return await self._request(Command.SET_SCENARIOS, p1=scenario_id)

    async def set_output(self, exit_id: int, value: int) -> object:
        return await self._request(Command.SET_OUTPUTS, p1=exit_id, p2=value)

    async def set_zone_excluded(self, zone_id: int, excluded: bool) -> object:
        return await self._request(Command.SET_ZONES, p1=zone_id, p2=1 if excluded else 0)

    async def clear_alarm_memory(self, area_id: int) -> object:
        return await self._request(Command.SET_PARTITIONS, p1=area_id, p2=int(ArmMode.CLEAR_MEMORY))
