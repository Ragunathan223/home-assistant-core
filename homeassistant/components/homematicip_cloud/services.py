"""Support for HomematicIP Cloud devices."""

from __future__ import annotations

import logging
from pathlib import Path

from homematicip.async_home import AsyncHome
from homematicip.base.helpers import handle_config
from homematicip.device import SwitchMeasuring
from homematicip.group import HeatingGroup
import voluptuous as vol

from homeassistant.const import ATTR_ENTITY_ID, ATTR_TEMPERATURE
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.config_validation import comp_entity_ids
from homeassistant.helpers.service import (
    async_register_admin_service,
    verify_domain_control,
)

from .const import DOMAIN
from .hap import HomematicIPConfigEntry

_LOGGER = logging.getLogger(__name__)

ATTR_ACCESSPOINT_ID = "accesspoint_id"
ATTR_ANONYMIZE = "anonymize"
ATTR_CLIMATE_PROFILE_INDEX = "climate_profile_index"
ATTR_CONFIG_OUTPUT_FILE_PREFIX = "config_output_file_prefix"
ATTR_CONFIG_OUTPUT_PATH = "config_output_path"
ATTR_DURATION = "duration"
ATTR_ENDTIME = "endtime"
ATTR_COOLING = "cooling"

DEFAULT_CONFIG_FILE_PREFIX = "hmip-config"

SERVICE_ACTIVATE_ECO_MODE_WITH_DURATION = "activate_eco_mode_with_duration"
SERVICE_ACTIVATE_ECO_MODE_WITH_PERIOD = "activate_eco_mode_with_period"
SERVICE_ACTIVATE_VACATION = "activate_vacation"
SERVICE_DEACTIVATE_ECO_MODE = "deactivate_eco_mode"
SERVICE_DEACTIVATE_VACATION = "deactivate_vacation"
SERVICE_DUMP_HAP_CONFIG = "dump_hap_config"
SERVICE_RESET_ENERGY_COUNTER = "reset_energy_counter"
SERVICE_SET_ACTIVE_CLIMATE_PROFILE = "set_active_climate_profile"
SERVICE_SET_HOME_COOLING_MODE = "set_home_cooling_mode"

HMIPC_SERVICES = [
    SERVICE_ACTIVATE_ECO_MODE_WITH_DURATION,
    SERVICE_ACTIVATE_ECO_MODE_WITH_PERIOD,
    SERVICE_ACTIVATE_VACATION,
    SERVICE_DEACTIVATE_ECO_MODE,
    SERVICE_DEACTIVATE_VACATION,
    SERVICE_DUMP_HAP_CONFIG,
    SERVICE_RESET_ENERGY_COUNTER,
    SERVICE_SET_ACTIVE_CLIMATE_PROFILE,
    SERVICE_SET_HOME_COOLING_MODE,
]

SCHEMA_ACTIVATE_ECO_MODE_WITH_DURATION = vol.Schema(
    {
        vol.Required(ATTR_DURATION): cv.positive_int,
        vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24)),
    }
)

SCHEMA_ACTIVATE_ECO_MODE_WITH_PERIOD = vol.Schema(
    {
        vol.Required(ATTR_ENDTIME): cv.datetime,
        vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24)),
    }
)

SCHEMA_ACTIVATE_VACATION = vol.Schema(
    {
        vol.Required(ATTR_ENDTIME): cv.datetime,
        vol.Required(ATTR_TEMPERATURE, default=18.0): vol.All(
            vol.Coerce(float), vol.Range(min=0, max=55)
        ),
        vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24)),
    }
)

SCHEMA_DEACTIVATE_ECO_MODE = vol.Schema(
    {vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24))}
)

SCHEMA_DEACTIVATE_VACATION = vol.Schema(
    {vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24))}
)

SCHEMA_SET_ACTIVE_CLIMATE_PROFILE = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): comp_entity_ids,
        vol.Required(ATTR_CLIMATE_PROFILE_INDEX): cv.positive_int,
    }
)

SCHEMA_DUMP_HAP_CONFIG = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_OUTPUT_PATH): cv.string,
        vol.Optional(
            ATTR_CONFIG_OUTPUT_FILE_PREFIX, default=DEFAULT_CONFIG_FILE_PREFIX
        ): cv.string,
        vol.Optional(ATTR_ANONYMIZE, default=True): cv.boolean,
    }
)

SCHEMA_RESET_ENERGY_COUNTER = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID): comp_entity_ids}
)

SCHEMA_SET_HOME_COOLING_MODE = vol.Schema(
    {
        vol.Optional(ATTR_COOLING, default=True): cv.boolean,
        vol.Optional(ATTR_ACCESSPOINT_ID): vol.All(str, vol.Length(min=24, max=24)),
    }
)


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Set up the HomematicIP Cloud services."""

    @verify_domain_control(hass, DOMAIN)
    async def async_call_hmipc_service(service: ServiceCall) -> None:
        """Call correct HomematicIP Cloud service."""
        service_name = service.service

        if service_name == SERVICE_ACTIVATE_ECO_MODE_WITH_DURATION:
            await _async_activate_eco_mode_with_duration(service)
        elif service_name == SERVICE_ACTIVATE_ECO_MODE_WITH_PERIOD:
            await _async_activate_eco_mode_with_period(service)
        elif service_name == SERVICE_ACTIVATE_VACATION:
            await _async_activate_vacation(service)
        elif service_name == SERVICE_DEACTIVATE_ECO_MODE:
            await _async_deactivate_eco_mode(service)
        elif service_name == SERVICE_DEACTIVATE_VACATION:
            await _async_deactivate_vacation(service)
        elif service_name == SERVICE_DUMP_HAP_CONFIG:
            await _async_dump_hap_config(service)
        elif service_name == SERVICE_RESET_ENERGY_COUNTER:
            await _async_reset_energy_counter(service)
        elif service_name == SERVICE_SET_ACTIVE_CLIMATE_PROFILE:
            await _set_active_climate_profile(service)
        elif service_name == SERVICE_SET_HOME_COOLING_MODE:
            await _async_set_home_cooling_mode(service)

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_ACTIVATE_ECO_MODE_WITH_DURATION,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_ACTIVATE_ECO_MODE_WITH_DURATION,
    )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_ACTIVATE_ECO_MODE_WITH_PERIOD,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_ACTIVATE_ECO_MODE_WITH_PERIOD,
    )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_ACTIVATE_VACATION,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_ACTIVATE_VACATION,
    )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_DEACTIVATE_ECO_MODE,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_DEACTIVATE_ECO_MODE,
    )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_DEACTIVATE_VACATION,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_DEACTIVATE_VACATION,
    )

    hass.services.async_register(
        domain=DOMAIN,
        service=SERVICE_SET_ACTIVE_CLIMATE_PROFILE,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_SET_ACTIVE_CLIMATE_PROFILE,
    )

    async_register_admin_service(
        hass=hass,
        domain=DOMAIN,
        service=SERVICE_DUMP_HAP_CONFIG,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_DUMP_HAP_CONFIG,
    )

    async_register_admin_service(
        hass=hass,
        domain=DOMAIN,
        service=SERVICE_RESET_ENERGY_COUNTER,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_RESET_ENERGY_COUNTER,
    )

    async_register_admin_service(
        hass=hass,
        domain=DOMAIN,
        service=SERVICE_SET_HOME_COOLING_MODE,
        service_func=async_call_hmipc_service,
        schema=SCHEMA_SET_HOME_COOLING_MODE,
    )


async def _async_activate_eco_mode_with_duration(service: ServiceCall) -> None:
    """Service to activate eco mode with duration."""
    duration = service.data[ATTR_DURATION]

    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.activate_absence_with_duration_async(duration)
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.activate_absence_with_duration_async(duration)


async def _async_activate_eco_mode_with_period(service: ServiceCall) -> None:
    """Service to activate eco mode with period."""
    endtime = service.data[ATTR_ENDTIME]

    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.activate_absence_with_period_async(endtime)
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.activate_absence_with_period_async(endtime)


async def _async_activate_vacation(service: ServiceCall) -> None:
    """Service to activate vacation."""
    endtime = service.data[ATTR_ENDTIME]
    temperature = service.data[ATTR_TEMPERATURE]

    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.activate_vacation_async(endtime, temperature)
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.activate_vacation_async(endtime, temperature)


async def _async_deactivate_eco_mode(service: ServiceCall) -> None:
    """Service to deactivate eco mode."""
    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.deactivate_absence_async()
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.deactivate_absence_async()


async def _async_deactivate_vacation(service: ServiceCall) -> None:
    """Service to deactivate vacation."""
    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.deactivate_vacation_async()
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.deactivate_vacation_async()


async def _set_active_climate_profile(service: ServiceCall) -> None:
    """Service to set the active climate profile."""
    entity_id_list = service.data[ATTR_ENTITY_ID]
    climate_profile_index = service.data[ATTR_CLIMATE_PROFILE_INDEX] - 1

    entry: HomematicIPConfigEntry
    for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
        if entity_id_list != "all":
            for entity_id in entity_id_list:
                group = entry.runtime_data.hmip_device_by_entity_id.get(entity_id)
                if group and isinstance(group, HeatingGroup):
                    await group.set_active_profile_async(climate_profile_index)
        else:
            for group in entry.runtime_data.home.groups:
                if isinstance(group, HeatingGroup):
                    await group.set_active_profile_async(climate_profile_index)


async def _async_dump_hap_config(service: ServiceCall) -> None:
    """Service to dump the configuration of a Homematic IP Access Point."""
    config_path: str = (
        service.data.get(ATTR_CONFIG_OUTPUT_PATH) or service.hass.config.config_dir
    )
    config_file_prefix = service.data[ATTR_CONFIG_OUTPUT_FILE_PREFIX]
    anonymize = service.data[ATTR_ANONYMIZE]

    entry: HomematicIPConfigEntry
    for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
        hap_sgtin = entry.unique_id
        assert hap_sgtin is not None

        if anonymize:
            hap_sgtin = hap_sgtin[-4:]

        file_name = f"{config_file_prefix}_{hap_sgtin}.json"
        path = Path(config_path)
        config_file = path / file_name

        json_state = await entry.runtime_data.home.download_configuration_async()
        json_state = handle_config(json_state, anonymize)

        config_file.write_text(json_state, encoding="utf8")


async def _async_reset_energy_counter(service: ServiceCall):
    """Service to reset the energy counter."""
    entity_id_list = service.data[ATTR_ENTITY_ID]

    entry: HomematicIPConfigEntry
    for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
        if entity_id_list != "all":
            for entity_id in entity_id_list:
                device = entry.runtime_data.hmip_device_by_entity_id.get(entity_id)
                if device and isinstance(device, SwitchMeasuring):
                    await device.reset_energy_counter_async()
        else:
            for device in entry.runtime_data.home.devices:
                if isinstance(device, SwitchMeasuring):
                    await device.reset_energy_counter_async()


async def _async_set_home_cooling_mode(service: ServiceCall):
    """Service to set the cooling mode."""
    cooling = service.data[ATTR_COOLING]

    if hapid := service.data.get(ATTR_ACCESSPOINT_ID):
        if home := _get_home(service.hass, hapid):
            await home.set_cooling_async(cooling)
    else:
        entry: HomematicIPConfigEntry
        for entry in service.hass.config_entries.async_loaded_entries(DOMAIN):
            await entry.runtime_data.home.set_cooling_async(cooling)


def _get_home(hass: HomeAssistant, hapid: str) -> AsyncHome | None:
    """Return a HmIP home."""
    entry: HomematicIPConfigEntry
    for entry in hass.config_entries.async_loaded_entries(DOMAIN):
        if entry.unique_id == hapid:
            return entry.runtime_data.home

    raise ServiceValidationError(
        translation_domain=DOMAIN,
        translation_key="access_point_not_found",
        translation_placeholders={"id": hapid},
    )
