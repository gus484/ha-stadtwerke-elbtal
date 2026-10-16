"""Async client for the Stadtwerke Elbtal customer portal.

Since October 2026 the portal is a Salesforce Experience Cloud site (LWR).
Data comes from Apex controller methods, all called through one endpoint:

    POST /webruntime/api/apex/execute?language=de&asGuest=…&htmlEncode=false
         {"namespace": "", "classname": "@udd/<class id>", "method": "…",
          "isContinuation": false, "params": {…}, "cacheable": false}

Observed login flow (HAR of 2026-10-16):

    GET  /login                          -> consent cookies
    POST apex CustomerPortalLoginController.login (as guest)
                                         -> {"returnValue": ".../frontdoor.jsp?…sid=…"}
    GET  frontdoor.jsp                   -> sets the session cookies (sid, …)
    GET  /                               -> HTML whose @app/user module holds
                                            the CSRF token every authenticated
                                            Apex call sends as ``CSRF-Token``

Session liveness has an explicit endpoint, ``SessionTimeServlet``, answering
``{"sr": <seconds left>}`` — ``-1`` once the session is gone. The portal's own
frontend polls it and logs out on ``sr <= 0``, so this client asks it before
doing any work rather than inferring expiry from what a data call returns.

This module deliberately knows nothing about Home Assistant. The caller owns
the :class:`aiohttp.ClientSession` and passes it in. Authentication is a set
of session cookies, so the session should not share its cookie jar with
unrelated code.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from typing import Any

import aiohttp
from yarl import URL

from .exceptions import ParseError, PortalError, SessionExpired
from .models import Contract, Meter, Reading
from .parsing import (
    INVALID_APEX_REQUEST,
    MeterPage,
    apex_error_message,
    apex_return_value,
    loads,
    parse_contracts,
    parse_login_response,
    parse_meter_readings,
    parse_portal_user,
    parse_session_remaining,
)

_LOGGER = logging.getLogger(__name__)

DEFAULT_HOST = "https://onlineservice.stadtwerke-elbtal.de"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:140.0) Gecko/20100101 Firefox/140.0"
)

APEX_PATH = "/webruntime/api/apex/execute"
SESSION_TIME_PATH = "/sfsites/c/_nc_external/system/security/session/SessionTimeServlet"

# Apex classes are addressed by record id, not by name. The names are what the
# portal's JavaScript registers them under (``@salesforce/apex/<Name>.<method>``)
# and are kept here so a changed id can be looked up again in the view bundles.
LOGIN_CLASS = "@udd/01pWz0000029p3P"  # CustomerPortalLoginController
CONTRACT_OVERVIEW_CLASS = "@udd/01pSe000000oqLh"  # OrderAndContractOverviewController
METER_READING_CLASS = "@udd/01pSe000002IBCD"  # PortalMeterReadingController

# Page sizes the portal's own frontend uses.
CONTRACT_PAGE_SIZE = 5
READING_PAGE_SIZE = 18
# For the current value. More than one, because a bidirectional meter lists
# its feed-in register alongside consumption and either may come first.
RECENT_PAGE_SIZE = 4
# A guard, not a limit anyone should reach: 50 readings is 3 pages.
MAX_PAGES = 20


class PortalClient:
    """Reads contracts, meters and readings from the customer portal.

    The session is supplied and owned by the caller::

        client = PortalClient(user, password, session)
        await client.async_ensure_session()
        contracts = await client.get_contracts()
        meters, recent = await client.get_meters_and_recent(contracts)
    """

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
        *,
        host: str = DEFAULT_HOST,
    ) -> None:
        self._username = username
        self._password = password
        self._host = host.rstrip("/")
        self._cookie_domain = URL(self._host).host or ""
        self._session = session
        self._csrf_token: str | None = None
        # Two coordinators share one client. Without the lock both could find
        # the session dead and log in at once, each clearing the cookies the
        # other has just been given.
        self._login_lock = asyncio.Lock()

    # -- introspection ------------------------------------------------------

    @property
    def host(self) -> str:
        return self._host

    @property
    def logged_in(self) -> bool:
        """True once a login has produced a CSRF token.

        Says nothing about whether the server still honours it; that is what
        :meth:`async_ensure_session` asks the portal.
        """
        return self._csrf_token is not None

    # -- low-level helpers --------------------------------------------------

    def _headers(self, **extra: str) -> dict[str, str]:
        return {"User-Agent": USER_AGENT, "Accept-Language": "de", **extra}

    async def _get_text(self, url: str) -> str:
        async with self._session.get(url, headers=self._headers()) as resp:
            if resp.status >= 500:
                raise PortalError(f"GET {URL(url).path}: HTTP {resp.status}")
            return await resp.text()

    async def _post_apex(
        self, classname: str, method: str, params: dict[str, Any] | None, *, guest: bool
    ) -> tuple[int, Any]:
        """POST one Apex call; return the HTTP status and the parsed body."""
        body: dict[str, Any] = {
            "namespace": "",
            "classname": classname,
            "method": method,
            "isContinuation": False,
            "cacheable": False,
        }
        if params is not None:
            body["params"] = params
        headers = self._headers(
            Accept="*/*",
            Origin=self._host,
            Referer=f"{self._host}/",
            # The frontend sends one per request; the server echoes it back,
            # which ties a logged failure to the portal's own request id.
            **{"X-SFDC-Request-Id": f"{int(time.time() * 1000)}{secrets.token_hex(3)}"},
        )
        if not guest:
            if self._csrf_token is None:
                raise SessionExpired("no CSRF token; not logged in")
            headers["CSRF-Token"] = self._csrf_token

        async with self._session.post(
            f"{self._host}{APEX_PATH}",
            params={
                "language": "de",
                "asGuest": "true" if guest else "false",
                "htmlEncode": "false",
            },
            json=body,
            headers=headers,
        ) as resp:
            status = resp.status
            content_type = resp.content_type
            text = await resp.text()

        if status >= 500:
            raise PortalError(f"{method}: HTTP {status}")
        if not guest and (status in (401, 403) or content_type != "application/json"):
            # Not a JSON answer to an authenticated call: the session is not
            # what the client thinks it is. Logged with the status because the
            # dead-session response of this endpoint has not been captured yet.
            _LOGGER.debug("%s -> HTTP %s, %s", method, status, content_type)
            raise SessionExpired(f"{method}: HTTP {status} ({content_type})")
        return status, loads(text, what=method)

    async def _apex(
        self, classname: str, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """An authenticated Apex call, re-logging in once on a dead session."""
        try:
            status, payload = await self._post_apex(classname, method, params, guest=False)
        except SessionExpired:
            _LOGGER.info("%s: session expired, re-logging in once", method)
            await self._relogin(stale_token=self._csrf_token)
            # A second SessionExpired propagates as a PortalError — transient
            # to the caller, never AuthError: the login just succeeded.
            status, payload = await self._post_apex(classname, method, params, guest=False)

        message = apex_error_message(payload)
        if message is not None:
            if message.strip() == INVALID_APEX_REQUEST:
                raise ParseError(f"{method}: {message} (portal Apex classes changed?)")
            raise PortalError(f"{method}: {message}")
        if status >= 400:
            raise PortalError(f"{method}: HTTP {status}")
        return apex_return_value(payload, what=method)

    # -- authentication -----------------------------------------------------

    async def login(self) -> None:
        """Authenticate. Raises :class:`AuthError` if the portal rejects it."""
        # Start from an empty jar for the portal, so the login is a fresh one
        # rather than a continuation of whatever session cookies are left.
        self._session.cookie_jar.clear_domain(self._cookie_domain)
        self._csrf_token = None

        # 1. The login page sets the consent cookies the browser carries into
        #    the login call.
        await self._get_text(f"{self._host}/login")

        # 2. The login itself, as guest.
        status, payload = await self._post_apex(
            LOGIN_CLASS,
            "login",
            {"username": self._username, "password": self._password, "startUrl": "/"},
            guest=True,
        )
        frontdoor = parse_login_response(status, payload, self._host)

        # 3. frontdoor.jsp turns the one-time sid into session cookies.
        await self._get_text(frontdoor)

        # 4. The authenticated start page carries the CSRF token.
        user = parse_portal_user(await self._get_text(f"{self._host}/"))
        if user.is_guest or user.csrf_token is None:
            # The credentials were accepted in step 2, so this is the portal
            # failing to hand over a session — not a reason to ask the user
            # for their password again.
            raise PortalError("login accepted, but the portal did not open a session")
        self._csrf_token = user.csrf_token

    async def session_seconds_left(self) -> int:
        """Seconds the portal session has left; ``<= 0`` means it is gone."""
        text = await self._get_text(
            f"{self._host}{SESSION_TIME_PATH}?buster={int(time.time() * 1000)}"
        )
        return parse_session_remaining(text)

    async def async_ensure_session(self) -> None:
        """Log in unless the portal confirms a live session.

        With a poll interval far beyond the ~2 h session lifetime this logs in
        on nearly every run — expiry is the normal case here, not an edge case.
        """
        async with self._login_lock:
            if self._csrf_token is not None and await self.session_seconds_left() > 0:
                return
            await self.login()

    async def _relogin(self, *, stale_token: str | None) -> None:
        """Log in again, unless a concurrent caller already replaced the token."""
        async with self._login_lock:
            if self._csrf_token is not None and self._csrf_token != stale_token:
                return
            await self.login()

    # -- data ---------------------------------------------------------------

    async def get_contracts(self) -> list[Contract]:
        """All supply contracts on the account, any medium."""
        contracts: list[Contract] = []
        for _ in range(MAX_PAGES):
            value = await self._apex(
                CONTRACT_OVERVIEW_CLASS,
                "loadOrderAndContractOverviewFiltered",
                {
                    "parameters": {
                        "numberOfItems": CONTRACT_PAGE_SIZE,
                        "offset": len(contracts),
                        "searchString": "",
                        "mediumElectricity": True,
                        "mediumGas": True,
                        "mediumHeat": True,
                        "mediumWater": True,
                        "status": "",
                        "contractPartner": "",
                        "types": ["ServicePointContract"],
                        "parentRecordId": None,
                    },
                    "context": "B2C",
                },
            )
            page, total = parse_contracts(value)
            contracts.extend(page)
            if not page or len(contracts) >= total:
                break
        return contracts

    async def _get_reading_page(
        self, contract: Contract, offset: int, size: int
    ) -> list[MeterPage]:
        value = await self._apex(
            METER_READING_CLASS,
            "callGetMeterReadingsWithParams",
            {
                "objectApiName": "ServiceContract",
                "recordId": contract.contract_id,
                "params": {"offset": offset, "numberOfItems": size},
            },
        )
        return parse_meter_readings(value, contract)

    async def get_meters_and_recent(
        self, contracts: list[Contract]
    ) -> tuple[list[Meter], list[Reading]]:
        """Meters under ``contracts`` and their most recent readings.

        One short page per contract — the portal lists readings newest first,
        so the current value is in it without paging through the history.
        """
        meters: list[Meter] = []
        recent: list[Reading] = []
        for contract in contracts:
            for page in await self._get_reading_page(contract, 0, RECENT_PAGE_SIZE):
                meters.append(page.meter)
                recent.extend(page.readings)
        return meters, recent

    async def get_readings(self, meter: Meter) -> list[Reading]:
        """The full reading history the portal holds for ``meter``."""
        contract = Contract(contract_id=meter.contract_id, medium=meter.medium, product_name="")
        readings: list[Reading] = []
        for _ in range(MAX_PAGES):
            pages = [
                p
                for p in await self._get_reading_page(contract, len(readings), READING_PAGE_SIZE)
                if p.meter.meter_no == meter.meter_no
            ]
            if not pages or not pages[0].readings:
                break
            readings.extend(pages[0].readings)
            if len(readings) >= pages[0].total:
                break
        else:
            _LOGGER.warning(
                "stopped after %d pages of readings; history may be incomplete", MAX_PAGES
            )
        return readings
