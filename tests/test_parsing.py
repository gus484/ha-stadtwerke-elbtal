"""Parser tests — the early-warning system for portal changes.

If the portal changes the shape of its Apex answers, the page that carries
the CSRF token, or the session servlet, these tests fail first.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from api.exceptions import AuthError, ParseError
from api.models import ReadingSource
from api.parsing import (
    loads,
    parse_contracts,
    parse_login_response,
    parse_meter_readings,
    parse_portal_user,
    parse_reading_source,
    parse_session_remaining,
)

from conftest import CONTRACT_ID, METER_NO, load, load_json

HOST = "https://onlineservice.stadtwerke-elbtal.de"


class TestLoads:
    def test_fractions_are_decimal(self) -> None:
        # 13114.7 as a float is 13114.699999999999…; the reading must arrive exact.
        assert loads('{"v": 13114.7}', what="t")["v"] == Decimal("13114.7")

    def test_strips_the_anti_hijacking_guard(self) -> None:
        assert loads('while(1);\n{"sr": 5}', what="t") == {"sr": 5}

    def test_html_is_a_parse_error(self) -> None:
        with pytest.raises(ParseError):
            loads("<html></html>", what="t")


class TestLogin:
    def test_frontdoor_url(self) -> None:
        url = parse_login_response(200, load_json("login_response.json"), HOST)
        assert url.startswith(f"{HOST}/vforcesite/secur/frontdoor.jsp?")
        assert "sid=" in url

    def test_relative_frontdoor_url_is_made_absolute(self) -> None:
        payload = {"returnValue": "/vforcesite/secur/frontdoor.jsp?sid=x"}
        assert parse_login_response(200, payload, HOST) == (
            f"{HOST}/vforcesite/secur/frontdoor.jsp?sid=x"
        )

    def test_rejected_login_is_an_auth_error(self) -> None:
        payload = {"error": [{"message": "Ihr Anmeldeversuch ist fehlgeschlagen."}]}
        with pytest.raises(AuthError):
            parse_login_response(400, payload, HOST)

    def test_invalid_apex_request_is_not_an_auth_error(self) -> None:
        # Captured verbatim from the portal for a call it did not accept. On
        # the login call it means the class moved — asking for the password
        # again would only add failed logins.
        payload = {"error": [{"message": "The Apex request is invalid."}]}
        with pytest.raises(ParseError):
            parse_login_response(400, payload, HOST)

    def test_frontdoor_on_another_host_is_refused(self) -> None:
        payload = {"returnValue": "https://evil.example/secur/frontdoor.jsp?sid=x"}
        with pytest.raises(ParseError):
            parse_login_response(200, payload, HOST)

    def test_return_value_that_is_no_frontdoor_url(self) -> None:
        with pytest.raises(ParseError):
            parse_login_response(200, {"returnValue": None}, HOST)


class TestPortalUser:
    def test_logged_in_page_carries_the_csrf_token(self) -> None:
        user = parse_portal_user(load("home.html"))
        assert user.is_guest is False
        assert user.csrf_token == "CSRF-TOKEN"

    def test_login_page_is_a_guest(self) -> None:
        user = parse_portal_user(load("login_page.html"))
        assert user.is_guest is True
        assert user.csrf_token is None

    def test_page_without_user_module(self) -> None:
        with pytest.raises(ParseError):
            parse_portal_user("<html><body></body></html>")


class TestSession:
    def test_alive(self) -> None:
        assert parse_session_remaining(load("session_alive.txt")) == 7198

    def test_expired(self) -> None:
        assert parse_session_remaining(load("session_expired.txt")) == -1


class TestContracts:
    def test_parses(self) -> None:
        contracts, total = parse_contracts(load_json("contracts.json")["returnValue"])
        assert total == 1
        assert len(contracts) == 1
        assert contracts[0].contract_id == CONTRACT_ID
        assert contracts[0].medium == "Electricity"
        assert contracts[0].product_name == "Mieterstrom"

    def test_unexpected_shape(self) -> None:
        with pytest.raises(ParseError):
            parse_contracts({"somethingElse": []})


class TestMeterReadings:
    @pytest.fixture
    def pages(self, contract):
        return parse_meter_readings(load_json("meter_readings.json")["returnValue"], contract)

    def test_meter(self, pages) -> None:
        assert len(pages) == 1
        meter = pages[0].meter
        assert meter.meter_no == METER_NO
        assert meter.contract_id == CONTRACT_ID
        assert meter.medium == "Electricity"
        assert meter.unit == "kWh"
        assert pages[0].total == 50

    def test_readings(self, pages) -> None:
        readings = pages[0].readings
        assert len(readings) == 18
        newest = readings[0]
        assert newest.reading_date == date(2026, 1, 31)
        assert newest.value == Decimal("13364")
        assert newest.unit == "kWh"
        assert newest.register == "1-1:1.8.0"
        assert newest.source is ReadingSource.READ
        assert readings[1].value == Decimal("13114.7")
        assert readings[-1].reading_date == date(2024, 7, 31)

    def test_values_are_exact(self, pages) -> None:
        for reading in pages[0].readings:
            assert isinstance(reading.value, Decimal)
            # Exactly one decimal place or none, as the portal sends them.
            assert reading.value == reading.value.quantize(Decimal("0.1"))

    def test_reading_without_value(self, contract) -> None:
        value = load_json("meter_readings.json")["returnValue"]
        value["meters"][0]["readings"][0]["readingValue"] = None
        with pytest.raises(ParseError):
            parse_meter_readings(value, contract)

    def test_no_meters_key(self, contract) -> None:
        with pytest.raises(ParseError):
            parse_meter_readings({"malo": "x"}, contract)


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Ablesung durch EVU", ReadingSource.READ),
        ("Ablesung durch Kunde", ReadingSource.SELF_REPORTED),
        ("Maschinelle Schätzung", ReadingSource.ESTIMATED),
        ("Irgendwas Neues", ReadingSource.UNKNOWN),
        (None, ReadingSource.UNKNOWN),
    ],
)
def test_reading_source(text, expected) -> None:
    assert parse_reading_source(text) is expected
