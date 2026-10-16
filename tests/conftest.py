"""Shared test helpers: load fixtures from tests/fixtures/."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from api.models import Contract, Reading
from api.parsing import parse_meter_readings

FIXTURES = Path(__file__).parent / "fixtures"

# The anonymised ids the fixtures were built with.
CONTRACT_ID = "810000000000001AAA"
METER_NO = "1ABC0000000001"


def load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json(name: str):
    return json.loads(load(name), parse_float=Decimal)


@pytest.fixture
def contract() -> Contract:
    return Contract(contract_id=CONTRACT_ID, medium="Electricity", product_name="Mieterstrom")


@pytest.fixture
def meter_history(contract: Contract) -> list[Reading]:
    """The captured page of readings, parsed — 18 monthly values."""
    value = load_json("meter_readings.json")["returnValue"]
    return parse_meter_readings(value, contract)[0].readings
