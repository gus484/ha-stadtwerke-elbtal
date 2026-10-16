"""Client flow tests using aioresponses against the fixtures."""

from __future__ import annotations

import copy
import json
import re

import aiohttp
import pytest
from aioresponses import aioresponses
from yarl import URL

from api import AuthError, ParseError, PortalClient, PortalError, SessionExpired
from api.client import APEX_PATH, SESSION_TIME_PATH

from conftest import CONTRACT_ID, METER_NO, load

HOST = "https://onlineservice.stadtwerke-elbtal.de"
APEX_RE = re.compile(re.escape(f"{HOST}{APEX_PATH}") + r"\?.*")
FRONTDOOR_RE = re.compile(re.escape(f"{HOST}/vforcesite/secur/frontdoor.jsp") + r"\?.*")
SESSION_RE = re.compile(re.escape(f"{HOST}{SESSION_TIME_PATH}") + r"\?.*")

LOGIN_REJECTED = {"error": [{"message": "Ihr Anmeldeversuch ist fehlgeschlagen."}]}
INVALID_APEX = {"error": [{"message": "The Apex request is invalid."}]}


def load_json(name: str):
    """Fixture JSON for aioresponses to serve — floats are fine on this side."""
    return json.loads(load(name))


def mock_login(m: aioresponses, *, home: str | None = None) -> None:
    """The four requests of a successful login, in order."""
    m.get(f"{HOST}/login", body=load("login_page.html"), content_type="text/html")
    m.post(APEX_RE, payload=load_json("login_response.json"))
    m.get(FRONTDOOR_RE, body="<html></html>", content_type="text/html")
    m.get(f"{HOST}/", body=home or load("home.html"), content_type="text/html")


def apex_calls(m: aioresponses) -> list[tuple[URL, dict]]:
    """Every Apex POST in the order sent, with its JSON body and headers."""
    calls = []
    for (method, url), requests in m.requests.items():
        if method == "POST" and url.path == APEX_PATH:
            calls.extend((url, r.kwargs) for r in requests)
    return calls


def readings_value(offset: int, size: int, total: int) -> dict:
    """A slice of the captured readings page, as the portal would page it."""
    value = copy.deepcopy(load_json("meter_readings.json")["returnValue"])
    meter = value["meters"][0]
    meter["readings"] = meter["readings"][offset : offset + size]
    meter["offset"] = offset
    meter["numberOfReadings"] = len(meter["readings"])
    meter["totalNumberOfReadings"] = total
    return {"returnValue": value}


@pytest.fixture
async def session():
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture
def client(session) -> PortalClient:
    """A client on a caller-owned session, the way the integration builds it."""
    return PortalClient("user@example.org", "secret", session)


# -- login -----------------------------------------------------------------


async def test_login_success(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
    assert client.logged_in is True


async def test_login_posts_as_guest_with_the_observed_fields(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        [(url, kwargs)] = apex_calls(m)
    assert url.query["asGuest"] == "true"
    assert "CSRF-Token" not in kwargs["headers"]
    body = kwargs["json"]
    assert body["classname"] == "@udd/01pWz0000029p3P"
    assert body["method"] == "login"
    assert body["params"] == {
        "username": "user@example.org",
        "password": "secret",
        "startUrl": "/",
    }


async def test_login_starts_from_an_empty_portal_cookie_jar(session) -> None:
    """Leftover session cookies must not survive into a new login."""
    session.cookie_jar.update_cookies({"sid": "stale"}, URL(HOST))
    session.cookie_jar.update_cookies({"other": "keep"}, URL("https://example.org/"))
    client = PortalClient("user", "secret", session)
    with aioresponses() as m:
        mock_login(m)
        await client.login()

    assert "sid" not in session.cookie_jar.filter_cookies(URL(HOST))
    # Only the portal's cookies go; the jar is not the client's to wipe.
    assert "other" in session.cookie_jar.filter_cookies(URL("https://example.org/"))


async def test_rejected_login_raises_auth_error(client) -> None:
    # Stand-in, not a capture: no failed login was recorded, to keep the
    # account clear of lock-relevant attempts. The Apex error envelope itself
    # is the captured one.
    with aioresponses() as m:
        m.get(f"{HOST}/login", body=load("login_page.html"), content_type="text/html")
        m.post(APEX_RE, status=400, payload=LOGIN_REJECTED)
        with pytest.raises(AuthError):
            await client.login()
    assert client.logged_in is False


async def test_vanished_login_class_is_not_an_auth_error(client) -> None:
    with aioresponses() as m:
        m.get(f"{HOST}/login", body=load("login_page.html"), content_type="text/html")
        m.post(APEX_RE, status=400, payload=INVALID_APEX)
        with pytest.raises(ParseError):
            await client.login()


async def test_accepted_login_without_session_is_not_an_auth_error(client) -> None:
    """Credentials were fine; the portal just did not hand over a session."""
    with aioresponses() as m:
        mock_login(m, home=load("login_page.html"))
        with pytest.raises(PortalError) as excinfo:
            await client.login()
    assert not isinstance(excinfo.value, AuthError)
    assert client.logged_in is False


# -- session ---------------------------------------------------------------


async def test_ensure_session_logs_in_when_never_logged_in(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.async_ensure_session()
    assert client.logged_in is True


async def test_ensure_session_keeps_a_live_session(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.get(SESSION_RE, body=load("session_alive.txt"), content_type="application/json")
        await client.async_ensure_session()
        logins = len(apex_calls(m))
    assert logins == 1


async def test_ensure_session_logs_in_again_when_expired(client) -> None:
    """Expiry is the normal case: polls are hours apart, the session lasts ~2 h."""
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.get(SESSION_RE, body=load("session_expired.txt"), content_type="application/json")
        mock_login(m)
        await client.async_ensure_session()
        logins = len(apex_calls(m))
    assert logins == 2


# -- data ------------------------------------------------------------------


async def test_get_contracts(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, payload=load_json("contracts.json"))
        contracts = await client.get_contracts()
        _, (url, kwargs) = apex_calls(m)
    assert [c.contract_id for c in contracts] == [CONTRACT_ID]
    assert url.query["asGuest"] == "false"
    assert kwargs["headers"]["CSRF-Token"] == "CSRF-TOKEN"
    assert kwargs["json"]["method"] == "loadOrderAndContractOverviewFiltered"


async def test_get_contracts_pages_until_total(client) -> None:
    entry = load_json("contracts.json")["returnValue"]["contractsAndOrders"][0]
    page = [dict(entry, relatedObjectId=f"810{i:015d}") for i in range(5)]
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, payload={"returnValue": {"contractsAndOrders": page, "totalItems": 6}})
        m.post(APEX_RE, payload={"returnValue": {
            "contractsAndOrders": [dict(entry, relatedObjectId="810999")], "totalItems": 6}})
        contracts = await client.get_contracts()
        offsets = [k["json"]["params"]["parameters"]["offset"] for _, k in apex_calls(m)[1:]]
    assert len(contracts) == 6
    assert offsets == [0, 5]


async def test_get_meters_and_recent(client, contract) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, payload=readings_value(0, 4, 50))
        meters, recent = await client.get_meters_and_recent([contract])
        _, (_, kwargs) = apex_calls(m)
    assert [x.meter_no for x in meters] == [METER_NO]
    assert len(recent) == 4
    assert kwargs["json"]["params"]["recordId"] == CONTRACT_ID
    assert kwargs["json"]["params"]["params"] == {"offset": 0, "numberOfItems": 4}


async def test_get_readings_pages_through_the_history(client, contract) -> None:
    meter_pages = readings_value(0, 1, 18)["returnValue"]
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, payload={"returnValue": meter_pages})
        [meter], _ = await client.get_meters_and_recent([contract])
        m.post(APEX_RE, payload=readings_value(0, 10, 18))
        m.post(APEX_RE, payload=readings_value(10, 10, 18))
        readings = await client.get_readings(meter)
        offsets = [k["json"]["params"]["params"]["offset"] for _, k in apex_calls(m)[2:]]
    assert len(readings) == 18
    assert offsets == [0, 10]


async def test_dead_session_on_a_data_call_relogs_once(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        # The dead-session answer of this endpoint is not captured; anything
        # that is not JSON must be read as "session gone", not as data.
        m.post(APEX_RE, status=401, body="<html></html>", content_type="text/html")
        mock_login(m)
        m.post(APEX_RE, payload=load_json("contracts.json"))
        contracts = await client.get_contracts()
        logins = [k for _, k in apex_calls(m) if k["json"]["method"] == "login"]
    assert [c.contract_id for c in contracts] == [CONTRACT_ID]
    assert len(logins) == 2


async def test_session_that_stays_dead_is_not_an_auth_error(client) -> None:
    """A fresh login succeeded, so the password is fine — no reauth dialog."""
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, status=401, body="<html></html>", content_type="text/html")
        mock_login(m)
        m.post(APEX_RE, status=401, body="<html></html>", content_type="text/html")
        with pytest.raises(SessionExpired) as excinfo:
            await client.get_contracts()
    assert not isinstance(excinfo.value, AuthError)


async def test_vanished_data_class_is_a_parse_error(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, status=400, payload=INVALID_APEX)
        with pytest.raises(ParseError):
            await client.get_contracts()


async def test_server_error_is_transient(client) -> None:
    with aioresponses() as m:
        mock_login(m)
        await client.login()
        m.post(APEX_RE, status=503, body="", content_type="text/html")
        with pytest.raises(PortalError) as excinfo:
            await client.get_contracts()
    assert not isinstance(excinfo.value, (AuthError, SessionExpired))
