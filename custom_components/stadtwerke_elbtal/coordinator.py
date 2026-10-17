"""Coordinators — two rhythms, not one.

The current reading is one short page per contract, so it is polled on a
regular interval. The full history pages through every reading the portal
holds and only feeds the statistics backfill, so it runs once at setup and
daily after that. Putting both on one interval would either hammer the portal
for the history or starve the current reading.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PortalClient
from .api.exceptions import AuthError, PortalError
from .api.models import Contract, Meter, Reading, mask_meter_no
from .const import (
    CONF_HISTORY_INTERVAL,
    CONF_METER_INTERVAL,
    CONSUMPTION_REGISTER,
    DEFAULT_HISTORY_INTERVAL_HOURS,
    DEFAULT_METER_INTERVAL_HOURS,
    DOMAIN,
    POWER_MEDIA,
)
from .statistics import async_import_readings

_LOGGER = logging.getLogger(__name__)

# Transport-level failures that mean "try again later", not "credentials wrong".
TRANSIENT_ERRORS = (PortalError, aiohttp.ClientError, TimeoutError)


def is_power_contract(contract: Contract) -> bool:
    """True for electricity contracts — the only ones worth fetching readings for."""
    return contract.medium in POWER_MEDIA


def is_power_meter(meter: Meter) -> bool:
    """True for electricity meters.

    A meter without a medium is judged by its unit instead, so the check stays
    about what a meter *is* rather than about which media we want to exclude.
    """
    if meter.medium:
        return meter.medium in POWER_MEDIA
    return meter.unit == UnitOfEnergy.KILO_WATT_HOUR


def is_consumption(reading: Reading) -> bool:
    """True for the consumed-energy register, or a reading that names none."""
    if reading.register is None:
        return True
    return reading.register.rpartition(":")[2] == CONSUMPTION_REGISTER


async def async_fetch_power_meters(
    client: PortalClient,
) -> tuple[list[Meter], list[Reading]]:
    """Electricity meters on the account and their recent readings.

    Shared by the meter coordinator and the config flow, so both agree on
    which meter an account has.
    """
    contracts = [c for c in await client.get_contracts() if is_power_contract(c)]
    meters, recent = await client.get_meters_and_recent(contracts)
    return [m for m in meters if is_power_meter(m)], recent


@dataclass(slots=True)
class MeterSnapshot:
    """What one poll of the current readings yields."""

    meters: list[Meter]
    latest: dict[str, Reading]


@dataclass(slots=True)
class ElbtalRuntimeData:
    """Everything a config entry owns at runtime."""

    client: PortalClient
    meter: "ElbtalMeterCoordinator"
    history: "ElbtalHistoryCoordinator"


type ElbtalConfigEntry = ConfigEntry[ElbtalRuntimeData]


class ElbtalBaseCoordinator[_DataT](DataUpdateCoordinator[_DataT]):
    """Shared session handling and error mapping."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ElbtalConfigEntry,
        client: PortalClient,
        *,
        name: str,
        interval: timedelta,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=name,
            update_interval=interval,
        )
        self.client = client

    async def _async_update_data(self) -> _DataT:
        try:
            await self.client.async_ensure_session()
            return await self._async_fetch()
        except AuthError as err:
            # Bubbles up as the reauth dialog rather than a silent retry loop;
            # repeated failed logins can lock the portal account.
            raise ConfigEntryAuthFailed(str(err)) from err
        except TRANSIENT_ERRORS as err:
            raise UpdateFailed(f"{self.name}: {err}") from err

    async def _async_fetch(self) -> _DataT:
        raise NotImplementedError


class ElbtalMeterCoordinator(ElbtalBaseCoordinator[MeterSnapshot]):
    """Polls the electricity meters and their current reading."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ElbtalConfigEntry,
        client: PortalClient,
    ) -> None:
        hours = entry.options.get(CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL_HOURS)
        super().__init__(
            hass,
            entry,
            client,
            name=f"{DOMAIN} meter",
            interval=timedelta(hours=hours),
        )

    async def _async_fetch(self) -> MeterSnapshot:
        meters, recent = await async_fetch_power_meters(self.client)
        latest: dict[str, Reading] = {}
        for reading in filter(is_consumption, recent):
            current = latest.get(reading.meter_no)
            if current is None or reading.reading_date > current.reading_date:
                latest[reading.meter_no] = reading
        return MeterSnapshot(meters=meters, latest=latest)


class ElbtalHistoryCoordinator(ElbtalBaseCoordinator[dict[str, list[Reading]]]):
    """Fetches the full reading history and backfills statistics.

    The import runs here rather than in a separate task because "fetch the
    history" and "write the history" share exactly one schedule; splitting
    them would mean keeping two timers in step for no gain.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ElbtalConfigEntry,
        client: PortalClient,
        meter_coordinator: ElbtalMeterCoordinator,
    ) -> None:
        hours = entry.options.get(CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL_HOURS)
        super().__init__(
            hass,
            entry,
            client,
            name=f"{DOMAIN} history",
            interval=timedelta(hours=hours),
        )
        self._meter_coordinator = meter_coordinator

    async def _async_fetch(self) -> dict[str, list[Reading]]:
        meters = (
            self._meter_coordinator.data.meters if self._meter_coordinator.data else []
        )
        if not meters:
            _LOGGER.debug("no electricity meter known yet, skipping history")
            return {}

        # Unlike the old meterDetails page, the portal now answers per contract
        # with the meter named, so every meter gets its own history.
        histories: dict[str, list[Reading]] = {}
        for meter in meters:
            readings = await self.client.get_readings(meter)
            consumption = [r for r in readings if is_consumption(r)]
            if len(consumption) != len(readings):
                _LOGGER.debug(
                    "meter %s: ignoring %d reading(s) of other registers",
                    mask_meter_no(meter.meter_no),
                    len(readings) - len(consumption),
                )
            await async_import_readings(
                self.hass,
                meter.meter_no,
                consumption,
                name=f"Strom {meter.meter_no}",
            )
            histories[meter.meter_no] = consumption
        return histories
