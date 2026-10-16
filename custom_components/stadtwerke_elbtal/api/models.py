"""Data models for contracts, meters and readings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum


class ReadingSource(str, Enum):
    """How the reading was obtained, from the portal's ``type`` field."""

    READ = "read"  # abgelesen, e.g. "Ablesung durch EVU"
    ESTIMATED = "estimated"  # geschätzt
    SELF_REPORTED = "self_reported"  # vom Kunden gemeldet
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Contract:
    """A supply contract (Servicevertrag) on the account."""

    contract_id: str  # Salesforce record id, e.g. "810…"
    medium: str  # e.g. "Electricity"
    product_name: str


@dataclass(frozen=True)
class Meter:
    """A single meter (Zähler) under a contract."""

    meter_no: str  # the serial number printed on the meter
    contract_id: str
    medium: str  # inherited from the contract, e.g. "Electricity"
    unit: str  # e.g. "kWh"


@dataclass(frozen=True)
class Reading:
    """One dated meter reading.

    ``value`` is a Decimal — cumulative meter readings must not accumulate
    float rounding error.
    """

    meter_no: str
    reading_date: date
    value: Decimal
    unit: str
    source: ReadingSource = ReadingSource.UNKNOWN
    # OBIS code of the register, e.g. "1-1:1.8.0" for consumed energy. A
    # bidirectional meter reports feed-in (2.8.0) under the same meter.
    register: str | None = None
