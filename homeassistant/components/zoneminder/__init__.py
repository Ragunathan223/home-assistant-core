"""Support for ZoneMinder."""

import logging

from requests.exceptions import ConnectionError as RequestsConnectionError
import voluptuous as vol
from zoneminder.zm import ZoneMinder

from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PATH,
    CONF_SSL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.discovery import async_load_platform
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN
from .services import async_setup_services

_LOGGER = logging.getLogger(__name__)

CONF_PATH_ZMS = "path_zms"

DEFAULT_PATH = "/zm/"
DEFAULT_PATH_ZMS = "/zm/cgi-bin/nph-zms"
DEFAULT_SSL = False
DEFAULT_TIMEOUT = 10
DEFAULT_VERIFY_SSL = True

HOST_CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_PASSWORD): cv.string,
        vol.Optional(CONF_PATH, default=DEFAULT_PATH): cv.string,
        vol.Optional(CONF_PATH_ZMS, default=DEFAULT_PATH_ZMS): cv.string,
        vol.Optional(CONF_SSL, default=DEFAULT_SSL): cv.boolean,
        vol.Optional(CONF_USERNAME): cv.string,
        vol.Optional(CONF_VERIFY_SSL, default=DEFAULT_VERIFY_SSL): cv.boolean,
    }
)

CONFIG_SCHEMA = vol.Schema(
    {DOMAIN: vol.All(cv.ensure_list, [HOST_CONFIG_SCHEMA])}, extra=vol.ALLOW_EXTRA
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the ZoneMinder component."""

    hass.data[DOMAIN] = {}

    success = True

    for conf in config[DOMAIN]:
        protocol = "https" if conf[CONF_SSL] else "http"

        host_name = conf[CONF_HOST]
        server_origin = f"{protocol}://{host_name}"
        zm_client = ZoneMinder(
            server_origin,
            conf.get(CONF_USERNAME),
            conf.get(CONF_PASSWORD),
            conf.get(CONF_PATH),
            conf.get(CONF_PATH_ZMS),
            conf.get(CONF_VERIFY_SSL),
        )
        hass.data[DOMAIN][host_name] = zm_client

        try:
            success = await hass.async_add_executor_job(zm_client.login) and success
        except RequestsConnectionError as ex:
            _LOGGER.error(
                "ZoneMinder connection failure to %s: %s",
                host_name,
                ex,
            )

    async_setup_services(hass)

    hass.async_create_task(
        async_load_platform(hass, Platform.BINARY_SENSOR, DOMAIN, {}, config)
    )

    return success
