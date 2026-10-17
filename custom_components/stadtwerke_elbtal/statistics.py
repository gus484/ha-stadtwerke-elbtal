"""Backfill historical readings as external statistics.

A normal sensor can only ever record the present, so the portal's reading
history — monthly readings reaching back several years — is written straight
into the recorder's statistics tables via :func:`async_add_external_statistics`.

The row rules — alignment, monotonic ``sum``, the zero baseline — live in
:mod:`.series` so they are testable without Home Assistant. This module adds
what needs the recorder: reading back the stored baseline and writing.

``Decimal`` all the way in, ``float`` only on the last line before handing a
value to the recorder. The portal's reading dates are bare calendar days, so
they are anchored at midnight in the *portal's* timezone, never in Home
Assistant's.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticData, StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    statistics_during_period,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util, slugify

from .api.models import Reading, mask_meter_no
from .const import DOMAIN, PORTAL_TIME_ZONE
from .series import build_series

_LOGGER = logging.getLogger(__name__)

# The recorder replaced ``has_mean`` with ``mean_type`` mid-2025 and older
# cores do not know the new key. Set whichever this core actually declares
# rather than guessing at the installed version.
_METADATA_KEYS = frozenset(getattr(StatisticMetaData, "__annotations__", {}))


def build_statistic_id(meter_no: str) -> str:
    """Return the external statistic id for a meter.

    External ids carry the domain and a colon — ``stadtwerke_elbtal:…`` —
    which is what marks them as not belonging to any entity.
    """
    return f"{DOMAIN}:{slugify(meter_no)}_energy"


async def _async_stored_baseline(
    hass: HomeAssistant, statistic_id: str
) -> Decimal | None:
    """The meter value the stored ``sum`` counts from, or ``None`` if empty.

    Every row is written as ``sum = state - baseline``, so the oldest stored
    row gives the baseline back. Reading it from the recorder instead of
    keeping it in the config entry means there is no second copy to drift.
    """
    stats = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        datetime.fromtimestamp(0, UTC),
        None,
        {statistic_id},
        "hour",
        None,
        {"state", "sum"},
    )
    rows = stats.get(statistic_id)
    if not rows:
        return None
    oldest = rows[0]
    if oldest.get("state") is None or oldest.get("sum") is None:
        return None
    # str() keeps the float's shortest repr, which is what was written.
    return Decimal(str(oldest["state"])) - Decimal(str(oldest["sum"]))


async def async_import_readings(
    hass: HomeAssistant,
    meter_no: str,
    readings: list[Reading],
    *,
    name: str | None = None,
) -> int:
    """Write ``readings`` for one meter as external statistics.

    Returns the number of rows written. Imports are keyed on
    ``(statistic_id, start)`` and the baseline is read back from the stored
    rows, so re-running this with the full history is idempotent and needs no
    diffing — even after the portal's window has moved past the first import.
    """
    statistic_id = build_statistic_id(meter_no)
    # The statistic id embeds the full meter number; logs get the short form.
    label = f"meter {mask_meter_no(meter_no)}"

    usable = [r for r in readings if r.unit == UnitOfEnergy.KILO_WATT_HOUR]
    if len(usable) != len(readings):
        _LOGGER.debug(
            "%s: ignoring %d reading(s) not in kWh",
            label,
            len(readings) - len(usable),
        )
    if not usable:
        _LOGGER.debug("%s: nothing to import", label)
        return 0

    tzinfo = await dt_util.async_get_time_zone(PORTAL_TIME_ZONE)
    baseline = await _async_stored_baseline(hass, statistic_id)
    points = build_series(usable, tzinfo, baseline, label=label)
    if not points:
        return 0
    # Decimal -> float happens here and nowhere earlier.
    rows = [
        StatisticData(start=p.start, state=float(p.state), sum=float(p.sum))
        for p in points
    ]

    metadata = StatisticMetaData(
        has_sum=True,
        name=name or f"{meter_no} Energie",
        source=DOMAIN,
        statistic_id=statistic_id,
        unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
    )
    if "mean_type" in _METADATA_KEYS:
        from homeassistant.components.recorder.models import (  # noqa: PLC0415
            StatisticMeanType,
        )

        metadata["mean_type"] = StatisticMeanType.NONE
    if "has_mean" in _METADATA_KEYS:
        metadata["has_mean"] = False
    if "unit_class" in _METADATA_KEYS:
        # The recorder needs the unit-conversion class so it can offer the
        # series in the user's preferred energy unit. Everything here is
        # filtered to kWh above, so this is always energy. Read the value off
        # the converter rather than hardcoding the "energy" string.
        from homeassistant.util.unit_conversion import (  # noqa: PLC0415
            EnergyConverter,
        )

        metadata["unit_class"] = EnergyConverter.UNIT_CLASS

    async_add_external_statistics(hass, metadata, rows)
    _LOGGER.debug(
        "%s: imported %d row(s), %s to %s, baseline %s",
        label,
        len(rows),
        rows[0]["start"],
        rows[-1]["start"],
        points[0].state - points[0].sum,
    )
    return len(rows)
