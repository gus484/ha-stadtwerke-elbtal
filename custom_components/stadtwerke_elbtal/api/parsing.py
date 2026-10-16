"""Parsers for the portal's JSON and the one HTML page that matters.

Kept separate from the client so they can be unit-tested against fixtures
without any network mocking. These parsers are the early-warning system for
portal changes.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from .exceptions import AuthError, ParseError
from .models import Contract, Meter, Reading, ReadingSource

# Salesforce's answer to an Apex call naming a class or method it does not
# know. A redeployed portal that renamed its controllers looks exactly like
# this, so it means "portal changed", never "wrong password".
INVALID_APEX_REQUEST = "The Apex request is invalid."

# The authenticated page defines the current user as an LWR module, CSRF
# token included: LWR.define('@app/user', [], function() { return {...}; });
_USER_MODULE_RE = re.compile(
    r"LWR\.define\('@app/user',\s*\[\],\s*function\(\)\s*\{\s*return\s*(\{.*?\});\s*\}\);"
)

# Salesforce prefixes JSON it serves to the browser with this anti-hijacking
# guard. It has to go before the payload parses.
_JSON_GUARD = "while(1);"

_UNITS = {"KWH": "kWh"}


def loads(text: str, *, what: str) -> Any:
    """Parse JSON with every fractional number as ``Decimal``.

    ``parse_float`` keeps meter readings exact from the first byte; a float
    detour would turn ``13114.7`` into ``13114.699999…`` before anything else
    got a chance.
    """
    text = text.strip()
    if text.startswith(_JSON_GUARD):
        text = text[len(_JSON_GUARD) :]
    try:
        return json.loads(text, parse_float=Decimal)
    except ValueError as exc:
        raise ParseError(f"{what} did not return JSON") from exc


def apex_error_message(payload: Any) -> str | None:
    """The first error message of an Apex error payload, if it is one.

    Errors come back as ``{"error": [{"message": "..."}]}``.
    """
    if not isinstance(payload, dict):
        return None
    errors = payload.get("error")
    if not isinstance(errors, list) or not errors:
        return None
    first = errors[0]
    if isinstance(first, dict) and isinstance(first.get("message"), str):
        return first["message"]
    return str(first)


def apex_return_value(payload: Any, *, what: str) -> Any:
    """Unwrap ``{"returnValue": ...}`` from a successful Apex call."""
    if not isinstance(payload, dict) or "returnValue" not in payload:
        keys = sorted(payload) if isinstance(payload, dict) else type(payload).__name__
        raise ParseError(f"{what}: no returnValue in response (top-level: {keys})")
    return payload["returnValue"]


def parse_login_response(status: int, payload: Any, host: str) -> str:
    """Return the frontdoor URL a successful login hands back.

    The login Apex method answers with a ``frontdoor.jsp`` URL carrying a
    one-time session id; requesting it is what sets the session cookies.

    A rejected login comes back as an Apex error. It is an :class:`AuthError`
    unless it is the "invalid request" error, which means the login class
    itself is gone and retyping the password would not help.
    """
    message = apex_error_message(payload)
    if message is not None:
        if message.strip() == INVALID_APEX_REQUEST:
            raise ParseError(f"login: {message} (portal Apex classes changed?)")
        raise AuthError(f"login rejected: {message}")
    if status >= 400:
        raise ParseError(f"login: HTTP {status} without an error message")

    url = apex_return_value(payload, what="login")
    if not isinstance(url, str) or "/secur/frontdoor.jsp" not in url:
        raise ParseError("login: returnValue is not a frontdoor URL")
    if url.startswith("/"):
        url = host + url
    if not url.startswith(host + "/"):
        # Never follow a session-bearing URL off the portal's own host.
        raise ParseError("login: frontdoor URL points to a different host")
    return url


@dataclass(frozen=True, slots=True)
class PortalUser:
    """What the authenticated page reveals about the session."""

    is_guest: bool
    csrf_token: str | None


def parse_portal_user(html: str) -> PortalUser:
    """Pull the ``@app/user`` module out of an LWR page."""
    match = _USER_MODULE_RE.search(html)
    if match is None:
        raise ParseError("page carries no @app/user module")
    try:
        user = json.loads(match.group(1))
    except ValueError as exc:
        raise ParseError("@app/user module is not JSON") from exc
    token = user.get("csrfToken")
    return PortalUser(
        is_guest=bool(user.get("isGuest", True)),
        csrf_token=token if isinstance(token, str) and token else None,
    )


def parse_session_remaining(text: str) -> int:
    """Seconds left in the session, from ``SessionTimeServlet``.

    ``{"sp": 7168, "sr": 7198}`` on a live session, ``{"sp": -1, "sr": -1}``
    once it is gone. The portal's own session validator logs out on
    ``sr <= 0``; the client does the same.
    """
    payload = loads(text, what="SessionTimeServlet")
    try:
        return int(payload["sr"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("SessionTimeServlet: no 'sr' field") from exc


def parse_contracts(value: Any) -> tuple[list[Contract], int]:
    """Contracts from ``loadOrderAndContractOverviewFiltered``, plus the total."""
    try:
        entries = value["contractsAndOrders"]
        total = int(value.get("totalItems", len(entries)))
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("contract overview: unexpected shape") from exc

    contracts = []
    for entry in entries:
        contract_id = entry.get("relatedObjectId")
        if not contract_id:
            raise ParseError("contract overview: entry without relatedObjectId")
        contracts.append(
            Contract(
                contract_id=contract_id,
                medium=entry.get("medium") or "",
                product_name=entry.get("productName") or "",
            )
        )
    return contracts, total


@dataclass(frozen=True, slots=True)
class MeterPage:
    """One meter's slice of a ``callGetMeterReadingsWithParams`` page."""

    meter: Meter
    readings: list[Reading]
    total: int  # readings the portal holds for this meter in all


def parse_meter_readings(value: Any, contract: Contract) -> list[MeterPage]:
    """Meters and readings from ``callGetMeterReadingsWithParams``."""
    try:
        meters = value["meters"]
    except (KeyError, TypeError) as exc:
        keys = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise ParseError(f"meter readings: no 'meters' (top-level: {keys})") from exc

    pages = []
    for raw in meters:
        meter_no = raw.get("serialNumber") or raw.get("deviceId")
        if not meter_no:
            raise ParseError("meter readings: meter without serialNumber")
        readings = [_parse_reading(r, meter_no) for r in raw.get("readings") or []]
        unit = readings[0].unit if readings else ""
        pages.append(
            MeterPage(
                meter=Meter(
                    meter_no=meter_no,
                    contract_id=contract.contract_id,
                    medium=contract.medium,
                    unit=unit,
                ),
                readings=readings,
                total=int(raw.get("totalNumberOfReadings") or 0),
            )
        )
    return pages


def _parse_reading(raw: dict, meter_no: str) -> Reading:
    try:
        reading_date = date.fromisoformat(raw["readingDate"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ParseError("meter reading without a valid readingDate") from exc

    value = raw.get("readingValue")
    # bool is an int subclass; a True here would be a schema change, not 1 kWh.
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ParseError(f"meter reading on {reading_date} has no numeric value")

    unit = str(raw.get("readingUnit") or raw.get("consumptionUnit") or "")
    return Reading(
        meter_no=meter_no,
        reading_date=reading_date,
        value=Decimal(value),
        unit=_UNITS.get(unit.upper(), unit),
        source=parse_reading_source(raw.get("type")),
        register=raw.get("obis") or None,
    )


def parse_reading_source(text: str | None) -> ReadingSource:
    """Map the portal's free-text reading type onto :class:`ReadingSource`.

    Only "Ablesung durch EVU" has been observed; the other branches match the
    obvious German wording and fall back to UNKNOWN rather than guessing.
    """
    if not text:
        return ReadingSource.UNKNOWN
    lowered = text.lower()
    if "schätz" in lowered or "schaetz" in lowered:
        return ReadingSource.ESTIMATED
    if "kunde" in lowered:
        return ReadingSource.SELF_REPORTED
    if "evu" in lowered or "ableser" in lowered or "netzbetreiber" in lowered:
        return ReadingSource.READ
    return ReadingSource.UNKNOWN
