"""Config flow for the Reolink camera component."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
import logging
from typing import Any

from reolink_aio.api import ALLOWED_SPECIAL_CHARS
from reolink_aio.baichuan import DEFAULT_BC_PORT
from reolink_aio.exceptions import (
    ApiError,
    CredentialsInvalidError,
    LoginFirmwareError,
    LoginPrivacyModeError,
    ReolinkError,
)
import voluptuous as vol

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_USERNAME,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import config_validation as cv, selector
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from .const import (
    CONF_BC_ONLY,
    CONF_BC_PORT,
    CONF_SUPPORTS_PRIVACY_MODE,
    CONF_USE_HTTPS,
    DOMAIN,
)
from .exceptions import (
    PasswordIncompatible,
    ReolinkException,
    ReolinkWebhookException,
    UserNotAdmin,
)
from .host import ReolinkHost
from .util import ReolinkConfigEntry, is_connected

_LOGGER = logging.getLogger(__name__)

DEFAULT_PROTOCOL = "rtsp"
DEFAULT_OPTIONS = {CONF_PROTOCOL: DEFAULT_PROTOCOL}
API_STARTUP_TIME = 5


class ReolinkOptionsFlowHandler(OptionsFlow):
    """Handle Reolink options."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the Reolink options."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_PROTOCOL,
                        default=self.config_entry.options[CONF_PROTOCOL],
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value="rtsp",
                                    label="RTSP",
                                ),
                                selector.SelectOptionDict(
                                    value="rtmp",
                                    label="RTMP",
                                ),
                                selector.SelectOptionDict(
                                    value="flv",
                                    label="FLV",
                                ),
                            ],
                        ),
                    ),
                }
            ),
        )


class ReolinkFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Reolink device."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize."""
        self._host: str | None = None
        self._username: str = "admin"
        self._password: str | None = None
        self._user_input: dict[str, Any] | None = None
        self._disable_privacy: bool = False

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ReolinkConfigEntry,
    ) -> ReolinkOptionsFlowHandler:
        """Options callback for Reolink."""
        return ReolinkOptionsFlowHandler()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Perform reauth upon an authentication error or no admin privileges."""
        self._host = entry_data[CONF_HOST]
        self._username = entry_data[CONF_USERNAME]
        self._password = entry_data[CONF_PASSWORD]
        placeholders = {
            **self.context["title_placeholders"],
            "ip_address": entry_data[CONF_HOST],
            "hostname": self.context["title_placeholders"]["name"],
        }
        self.context["title_placeholders"] = placeholders
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform a reauthentication."""
        return await self.async_step_user()

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Perform a reconfiguration."""
        entry_data = self._get_reconfigure_entry().data
        self._host = entry_data[CONF_HOST]
        self._username = entry_data[CONF_USERNAME]
        self._password = entry_data[CONF_PASSWORD]
        return await self.async_step_user()

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle discovery via dhcp."""
        mac_address = format_mac(discovery_info.macaddress)
        existing_entry = await self.async_set_unique_id(mac_address)
        if (
            existing_entry
            and CONF_PASSWORD in existing_entry.data
            and existing_entry.data[CONF_HOST] != discovery_info.ip
        ):
            if is_connected(self.hass, existing_entry):
                _LOGGER.debug(
                    "Reolink DHCP reported new IP '%s', "
                    "but connection to camera seems to be okay, so sticking to IP '%s'",
                    discovery_info.ip,
                    existing_entry.data[CONF_HOST],
                )
                raise AbortFlow("already_configured")

            # check if the camera is reachable at the new IP
            new_config = dict(existing_entry.data)
            new_config[CONF_HOST] = discovery_info.ip
            host = ReolinkHost(self.hass, new_config, existing_entry.options)
            try:
                await host.api.get_state("GetLocalLink")
                await host.api.logout()
            except ReolinkError as err:
                _LOGGER.debug(
                    "Reolink DHCP reported new IP '%s', "
                    "but got error '%s' trying to connect, so sticking to IP '%s'",
                    discovery_info.ip,
                    err,
                    existing_entry.data[CONF_HOST],
                )
                raise AbortFlow("already_configured") from err
            if format_mac(host.api.mac_address) != mac_address:
                _LOGGER.debug(
                    "Reolink mac address '%s' at new IP '%s' from DHCP, "
                    "does not match mac '%s' of config entry, so sticking to IP '%s'",
                    format_mac(host.api.mac_address),
                    discovery_info.ip,
                    mac_address,
                    existing_entry.data[CONF_HOST],
                )
                raise AbortFlow("already_configured")

        if existing_entry and existing_entry.data[CONF_HOST] != discovery_info.ip:
            _LOGGER.debug(
                "Reolink DHCP reported new IP '%s', updating from old IP '%s'",
                discovery_info.ip,
                existing_entry.data[CONF_HOST],
            )

        self._abort_if_unique_id_configured(updates={CONF_HOST: discovery_info.ip})

        self.context["title_placeholders"] = {
            "ip_address": discovery_info.ip,
            "hostname": discovery_info.hostname,
        }

        self._host = discovery_info.ip
        return await self.async_step_user()

    async def async_step_privacy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask permission to disable privacy mode."""
        if user_input is not None:
            self._disable_privacy = True
            return await self.async_step_user(self._user_input)

        assert self._user_input is not None
        placeholders = {"host": self._user_input[CONF_HOST]}
        return self.async_show_form(
            step_id="privacy",
            description_placeholders=placeholders,
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        errors = {}
        placeholders = {
            "error": "",
            "troubleshooting_link": "https://www.home-assistant.io/integrations/reolink/#troubleshooting",
        }

        if user_input is not None:
            if CONF_HOST not in user_input:
                user_input[CONF_HOST] = self._host

            # remember input in case of a error
            self._username = user_input[CONF_USERNAME]
            self._password = user_input[CONF_PASSWORD]
            self._host = user_input[CONF_HOST]

            host = ReolinkHost(self.hass, user_input, DEFAULT_OPTIONS)
            try:
                if self._disable_privacy:
                    await host.api.baichuan.set_privacy_mode(enable=False)
                    # give the camera some time to startup the HTTP API server
                    await asyncio.sleep(API_STARTUP_TIME)
                await host.async_init()
            except UserNotAdmin:
                errors[CONF_USERNAME] = "not_admin"
                placeholders["username"] = host.api.username
                placeholders["userlevel"] = host.api.user_level
            except PasswordIncompatible:
                errors[CONF_PASSWORD] = "password_incompatible"
                placeholders["special_chars"] = ALLOWED_SPECIAL_CHARS
            except LoginPrivacyModeError:
                self._user_input = user_input
                return await self.async_step_privacy()
            except CredentialsInvalidError:
                errors[CONF_PASSWORD] = "invalid_auth"
            except LoginFirmwareError:
                errors["base"] = "update_needed"
                placeholders["current_firmware"] = host.api.sw_version
                placeholders["needed_firmware"] = (
                    host.api.sw_version_required.version_string
                )
                placeholders["download_center_url"] = (
                    "https://reolink.com/download-center"
                )
            except ApiError as err:
                placeholders["error"] = str(err)
                errors[CONF_HOST] = "api_error"
            except ReolinkWebhookException as err:
                placeholders["error"] = str(err)
                placeholders["more_info"] = (
                    "https://www.home-assistant.io/more-info/no-url-available/#configuring-the-instance-url"
                )
                errors["base"] = "webhook_exception"
            except (ReolinkError, ReolinkException) as err:
                placeholders["error"] = str(err)
                errors[CONF_HOST] = "cannot_connect"
            except Exception as err:
                _LOGGER.exception("Unexpected exception")
                placeholders["error"] = str(err)
                errors[CONF_HOST] = "unknown"
            finally:
                await host.stop()

            if not errors:
                user_input[CONF_PORT] = host.api.port
                user_input[CONF_USE_HTTPS] = host.api.use_https
                user_input[CONF_BC_PORT] = host.api.baichuan.port
                user_input[CONF_BC_ONLY] = host.api.baichuan_only
                user_input[CONF_SUPPORTS_PRIVACY_MODE] = host.api.supported(
                    None, "privacy_mode"
                )

                mac_address = format_mac(host.api.mac_address)
                await self.async_set_unique_id(mac_address, raise_on_progress=False)
                if self.source == SOURCE_REAUTH:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry=self._get_reauth_entry(), data=user_input
                    )
                if self.source == SOURCE_RECONFIGURE:
                    self._abort_if_unique_id_mismatch()
                    return self.async_update_reload_and_abort(
                        entry=self._get_reconfigure_entry(), data=user_input
                    )
                self._abort_if_unique_id_configured()

                return self.async_create_entry(
                    title=str(host.api.nvr_name),
                    data=user_input,
                    options=DEFAULT_OPTIONS,
                )

        data_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME, default=self._username): str,
                vol.Required(CONF_PASSWORD, default=self._password): str,
            }
        )
        if self._host is None or self.source == SOURCE_RECONFIGURE or errors:
            data_schema = data_schema.extend(
                {
                    vol.Required(CONF_HOST, default=self._host): str,
                }
            )
        if errors:
            data_schema = data_schema.extend(
                {
                    vol.Optional(CONF_PORT): cv.port,
                    vol.Required(CONF_USE_HTTPS, default=False): bool,
                    vol.Required(CONF_BC_PORT, default=DEFAULT_BC_PORT): cv.port,
                }
            )

        return self.async_show_form(
            step_id="user",
            data_schema=data_schema,
            errors=errors,
            description_placeholders=placeholders,
        )
