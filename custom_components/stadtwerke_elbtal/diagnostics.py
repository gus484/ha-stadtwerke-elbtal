"""Diagnostics, redacted over the same field list as the test fixtures."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from .const import REDACT_KEYS
from .coordinator import ElbtalConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ElbtalConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry.

    Diagnostics get attached to bug reports, so everything that identifies the
    customer or the installation goes through the same redaction list the
    fixtures were anonymised with. The statistic id is left out entirely
    because it embeds the meter number.
    """
    data = entry.runtime_data
    meter = data.meter
    history = data.history

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), REDACT_KEYS),
            "options": dict(entry.options),
        },
        "portal": {
            "host": data.client.host,
            "logged_in": data.client.logged_in,
        },
        "meter_coordinator": {
            "last_update_success": meter.last_update_success,
            "update_interval": str(meter.update_interval),
            "meters": [
                async_redact_data(asdict(m), REDACT_KEYS)
                for m in (meter.data.meters if meter.data else [])
            ],
            "latest_readings": [
                async_redact_data(_reading_dict(r), REDACT_KEYS)
                for r in (meter.data.latest.values() if meter.data else [])
            ],
        },
        "history_coordinator": {
            "last_update_success": history.last_update_success,
            "update_interval": str(history.update_interval),
            # Keyed by position, not meter number — the key would leak it.
            "meters": [
                {
                    "reading_count": len(readings),
                    "readings": [
                        async_redact_data(_reading_dict(r), REDACT_KEYS)
                        for r in readings
                    ],
                }
                for readings in (history.data or {}).values()
            ],
        },
    }


def _reading_dict(reading) -> dict[str, Any]:
    """A JSON-safe reading; ``value`` stays exact by going out as a string."""
    return {
        "meter_no": reading.meter_no,
        "reading_date": reading.reading_date.isoformat(),
        "value": str(reading.value),
        "unit": reading.unit,
        "register": reading.register,
        "source": reading.source.value,
    }
