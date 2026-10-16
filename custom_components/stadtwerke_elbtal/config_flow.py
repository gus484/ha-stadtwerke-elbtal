"""Config flow for Stadtwerke Elbtal."""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import (
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import PortalClient
from .api.exceptions import AuthError, PortalError
from .const import (
    CONF_HISTORY_INTERVAL,
    CONF_METER_INTERVAL,
    DEFAULT_HISTORY_INTERVAL_HOURS,
    DEFAULT_METER_INTERVAL_HOURS,
    DOMAIN,
    MANUFACTURER,
    MAX_INTERVAL_HOURS,
    MIN_METER_INTERVAL_HOURS,
)
from .coordinator import async_fetch_power_meters

_LOGGER = logging.getLogger(__name__)

PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _credentials_schema(username: str | None = None) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_USERNAME, default=username or vol.UNDEFINED): TextSelector(
                TextSelectorConfig(type=TextSelectorType.EMAIL, autocomplete="username")
            ),
            vol.Required(CONF_PASSWORD): PASSWORD_SELECTOR,
        }
    )


class NoElectricityMeter(Exception):
    """The account authenticated but has no electricity meter on it."""


async def async_validate_login(hass: HomeAssistant, username: str, password: str) -> str:
    """Log in for real and return the electricity meter number.

    A config flow that only checked the form fields would happily store
    credentials the portal rejects.
    """
    # A throwaway session, so the validation login's cookies end up neither in
    # HA's shared jar nor in the entry's session.
    session = async_create_clientsession(hass, auto_cleanup=False)
    try:
        client = PortalClient(username, password, session)
        await client.login()
        meters, _ = await async_fetch_power_meters(client)
    finally:
        session.detach()
    if not meters:
        raise NoElectricityMeter
    return meters[0].meter_no


def _error_key(err: Exception) -> str:
    if isinstance(err, AuthError):
        return "invalid_auth"
    if isinstance(err, NoElectricityMeter):
        return "no_meter"
    if isinstance(err, (PortalError, aiohttp.ClientError, TimeoutError)):
        return "cannot_connect"
    _LOGGER.exception("unexpected error validating portal login", exc_info=err)
    return "unknown"


def _interval_selector(minimum: int) -> NumberSelector:
    return NumberSelector(
        NumberSelectorConfig(
            min=minimum,
            max=MAX_INTERVAL_HOURS,
            step=1,
            mode=NumberSelectorMode.BOX,
            unit_of_measurement="h",
        )
    )


class ElbtalConfigFlow(ConfigFlow, domain=DOMAIN):
    """Credentials, validated by a real login."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the portal login and verify it."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                meter_no = await async_validate_login(
                    self.hass, user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
                )
            except Exception as err:  # noqa: BLE001 — mapped, unknown ones logged
                errors["base"] = _error_key(err)
            else:
                # Without this the same installation can be added twice, and
                # the two entries then fight over one set of statistics.
                await self.async_set_unique_id(meter_no)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=f"{MANUFACTURER} ({meter_no})",
                    data={
                        CONF_USERNAME: user_input[CONF_USERNAME],
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=_credentials_schema(
                user_input.get(CONF_USERNAME) if user_input else None
            ),
            errors=errors,
        )

    # -- reauth ------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Triggered by ConfigEntryAuthFailed from either coordinator."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again and verify it before storing it."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        username = entry.data[CONF_USERNAME]

        if user_input is not None:
            username = user_input.get(CONF_USERNAME, username)
            try:
                await async_validate_login(
                    self.hass, username, user_input[CONF_PASSWORD]
                )
            except Exception as err:  # noqa: BLE001 — mapped, unknown ones logged
                errors["base"] = _error_key(err)
            else:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_USERNAME: username,
                        CONF_PASSWORD: user_input[CONF_PASSWORD],
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_credentials_schema(username),
            errors=errors,
            description_placeholders={"username": username},
        )

    # -- options -----------------------------------------------------------

    @staticmethod
    def async_get_options_flow(config_entry) -> ElbtalOptionsFlow:
        return ElbtalOptionsFlow()


class ElbtalOptionsFlow(OptionsFlow):
    """Lets the user slow down or speed up the two polls independently."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(
                data={
                    CONF_METER_INTERVAL: int(user_input[CONF_METER_INTERVAL]),
                    CONF_HISTORY_INTERVAL: int(user_input[CONF_HISTORY_INTERVAL]),
                }
            )

        options = self.config_entry.options
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_METER_INTERVAL,
                        default=options.get(
                            CONF_METER_INTERVAL, DEFAULT_METER_INTERVAL_HOURS
                        ),
                    ): _interval_selector(MIN_METER_INTERVAL_HOURS),
                    vol.Required(
                        CONF_HISTORY_INTERVAL,
                        default=options.get(
                            CONF_HISTORY_INTERVAL, DEFAULT_HISTORY_INTERVAL_HOURS
                        ),
                    ): _interval_selector(DEFAULT_HISTORY_INTERVAL_HOURS),
                }
            ),
        )
