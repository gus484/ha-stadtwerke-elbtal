"""Constants for the Stadtwerke Elbtal integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "stadtwerke_elbtal"
MANUFACTURER: Final = "Stadtwerke Elbtal"

# -- options keys ----------------------------------------------------------

CONF_METER_INTERVAL: Final = "meter_interval_hours"
CONF_HISTORY_INTERVAL: Final = "history_interval_hours"

# -- polling ---------------------------------------------------------------

# The portal updates readings monthly at best, so twice a day is already
# generous for the current value. The history pages through every reading
# the portal holds and only feeds the statistics backfill — daily is plenty.
DEFAULT_METER_INTERVAL_HOURS: Final = 12
DEFAULT_HISTORY_INTERVAL_HOURS: Final = 24
MIN_METER_INTERVAL_HOURS: Final = 1
MAX_INTERVAL_HOURS: Final = 168

# -- portal ----------------------------------------------------------------

# Contract media we produce entities and statistics for. Electricity only;
# other media are ignored rather than mis-modelled.
POWER_MEDIA: Final[frozenset[str]] = frozenset({"Electricity"})

# OBIS register for consumed energy, total over all tariffs. The medium and
# channel before the colon vary between meters ("1-0:", "1-1:"), so only the
# part after it is compared — as a whole, since "21.8.0" (phase L1) also ends
# in "1.8.0". Feed-in (2.8.0) would otherwise be summed into consumption.
CONSUMPTION_REGISTER: Final = "1.8.0"

# Reading dates are bare calendar days from a German portal, so they are
# anchored in the portal's timezone, not in Home Assistant's.
PORTAL_TIME_ZONE: Final = "Europe/Berlin"

# -- diagnostics / fixtures ------------------------------------------------

# The single field list used by both diagnostics redaction and the fixture
# anonymisation, so the two cannot drift apart.
REDACT_KEYS: Final[frozenset[str]] = frozenset(
    {
        "password",
        "username",
        "cookie",
        "cookies",
        "authorization",
        "csrfToken",
        "csrf_token",
        "meter_no",
        "serialNumber",
        "deviceId",
        "contract_id",
        "contractId",
        "recordId",
        "relatedObjectId",
        "contractNumber",
        "contractAccountNumber",
        "recordName",
        "malo",
        "marketIdentifier",
        "contractPartner",
        "accountName",
        "name",
        "firstName",
        "lastName",
        "street",
        "houseNumber",
        "postalCode",
        "city",
        "email",
        "phone",
        "iban",
    }
)
