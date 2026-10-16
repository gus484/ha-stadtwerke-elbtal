"""Sensor for the current meter reading."""

from __future__ import annotations

import logging
from decimal import Decimal

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api.models import Meter, ReadingSource
from .const import DOMAIN, MANUFACTURER
from .coordinator import ElbtalConfigEntry, ElbtalMeterCoordinator

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ElbtalConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one reading sensor per electricity meter."""
    coordinator = entry.runtime_data.meter
    meters = coordinator.data.meters if coordinator.data else []
    if not meters:
        _LOGGER.warning("no electricity meter found on this portal account")
    async_add_entities(ElbtalMeterReadingSensor(coordinator, meter) for meter in meters)


class ElbtalMeterReadingSensor(CoordinatorEntity[ElbtalMeterCoordinator], SensorEntity):
    """The meter's current cumulative reading (Zählerstand)."""

    _attr_has_entity_name = True
    _attr_translation_key = "meter_reading"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_state_class = SensorStateClass.TOTAL_INCREASING
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

    def __init__(self, coordinator: ElbtalMeterCoordinator, meter: Meter) -> None:
        super().__init__(coordinator)
        self._meter_no = meter.meter_no
        # Keyed on the meter number, not the config entry: re-adding the entry
        # must land on the same entity rather than creating a second one.
        self._attr_unique_id = f"{meter.meter_no}_energy"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, meter.meter_no)},
            name=f"Zähler {meter.meter_no}",
            manufacturer=MANUFACTURER,
            model="Stromzähler",
            serial_number=meter.meter_no,
        )

    @property
    def _reading(self):
        data = self.coordinator.data
        return data.latest.get(self._meter_no) if data else None

    @property
    def available(self) -> bool:
        return super().available and self._reading is not None

    @property
    def native_value(self) -> Decimal | None:
        """The reading as a Decimal — no float conversion on this path."""
        reading = self._reading
        return reading.value if reading else None

    @property
    def extra_state_attributes(self) -> dict[str, str]:
        reading = self._reading
        if reading is None:
            return {}
        attributes = {
            "meter_number": reading.meter_no,
            "reading_date": reading.reading_date.isoformat(),
        }
        if reading.source is not ReadingSource.UNKNOWN:
            attributes["reading_source"] = reading.source.value
        return attributes
