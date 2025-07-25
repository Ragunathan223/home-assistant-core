"""Config flow for the Home Assistant Yellow integration."""

from __future__ import annotations

from abc import ABC, abstractmethod
import asyncio
import logging
from typing import TYPE_CHECKING, Any, Protocol, final

import aiohttp
import voluptuous as vol

from homeassistant.components.hassio import (
    HassioAPIError,
    async_get_yellow_settings,
    async_set_yellow_settings,
    get_supervisor_client,
)
from homeassistant.components.homeassistant_hardware.firmware_config_flow import (
    BaseFirmwareConfigFlow,
    BaseFirmwareOptionsFlow,
)
from homeassistant.components.homeassistant_hardware.silabs_multiprotocol_addon import (
    OptionsFlowHandler as MultiprotocolOptionsFlowHandler,
    SerialPortSettings as MultiprotocolSerialPortSettings,
)
from homeassistant.components.homeassistant_hardware.util import (
    ApplicationType,
    FirmwareInfo,
)
from homeassistant.config_entries import (
    SOURCE_HARDWARE,
    ConfigEntry,
    ConfigEntryBaseFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import HomeAssistant, async_get_hass, callback
from homeassistant.helpers import discovery_flow, selector

from .const import (
    DOMAIN,
    FIRMWARE,
    FIRMWARE_VERSION,
    NABU_CASA_FIRMWARE_RELEASES_URL,
    RADIO_DEVICE,
    ZHA_DOMAIN,
    ZHA_HW_DISCOVERY_DATA,
)
from .hardware import BOARD_NAME

_LOGGER = logging.getLogger(__name__)

STEP_HW_SETTINGS_SCHEMA = vol.Schema(
    {
        vol.Required("disk_led"): selector.BooleanSelector(),
        vol.Required("heartbeat_led"): selector.BooleanSelector(),
        vol.Required("power_led"): selector.BooleanSelector(),
    }
)

if TYPE_CHECKING:

    class FirmwareInstallFlowProtocol(Protocol):
        """Protocol describing `BaseFirmwareInstallFlow` for a mixin."""

        async def _install_firmware_step(
            self,
            fw_update_url: str,
            fw_type: str,
            firmware_name: str,
            expected_installed_firmware_type: ApplicationType,
            step_id: str,
            next_step_id: str,
        ) -> ConfigFlowResult: ...

else:
    # Multiple inheritance with `Protocol` seems to break
    FirmwareInstallFlowProtocol = object


class YellowFirmwareMixin(ConfigEntryBaseFlow, FirmwareInstallFlowProtocol):
    """Mixin for Home Assistant Yellow firmware methods."""

    async def async_step_install_zigbee_firmware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Install Zigbee firmware."""
        return await self._install_firmware_step(
            fw_update_url=NABU_CASA_FIRMWARE_RELEASES_URL,
            fw_type="yellow_zigbee_ncp",
            firmware_name="Zigbee",
            expected_installed_firmware_type=ApplicationType.EZSP,
            step_id="install_zigbee_firmware",
            next_step_id="confirm_zigbee",
        )

    async def async_step_install_thread_firmware(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Install Thread firmware."""
        return await self._install_firmware_step(
            fw_update_url=NABU_CASA_FIRMWARE_RELEASES_URL,
            fw_type="yellow_openthread_rcp",
            firmware_name="OpenThread",
            expected_installed_firmware_type=ApplicationType.SPINEL,
            step_id="install_thread_firmware",
            next_step_id="start_otbr_addon",
        )


class HomeAssistantYellowConfigFlow(
    YellowFirmwareMixin, BaseFirmwareConfigFlow, domain=DOMAIN
):
    """Handle a config flow for Home Assistant Yellow."""

    VERSION = 1
    MINOR_VERSION = 4

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Instantiate config flow."""
        super().__init__(*args, **kwargs)

        self._device = RADIO_DEVICE

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlow:
        """Return the options flow."""
        firmware_type = ApplicationType(config_entry.data[FIRMWARE])
        hass = async_get_hass()

        if firmware_type is ApplicationType.CPC:
            return HomeAssistantYellowMultiPanOptionsFlowHandler(hass, config_entry)

        return HomeAssistantYellowOptionsFlowHandler(hass, config_entry)

    async def async_step_system(
        self, data: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        # We do not actually use any portion of `BaseFirmwareConfigFlow` beyond this
        await self._probe_firmware_info()

        # Kick off ZHA hardware discovery automatically if Zigbee firmware is running
        if (
            self._probed_firmware_info is not None
            and self._probed_firmware_info.firmware_type is ApplicationType.EZSP
        ):
            discovery_flow.async_create_flow(
                self.hass,
                ZHA_DOMAIN,
                context={"source": SOURCE_HARDWARE},
                data=ZHA_HW_DISCOVERY_DATA,
            )

        return self._async_flow_finished()

    def _async_flow_finished(self) -> ConfigFlowResult:
        """Create the config entry."""
        return self.async_create_entry(
            title=BOARD_NAME,
            data={
                # Assume the firmware type is EZSP if we cannot probe it
                FIRMWARE: (
                    self._probed_firmware_info.firmware_type
                    if self._probed_firmware_info is not None
                    else ApplicationType.EZSP
                ).value,
                FIRMWARE_VERSION: (
                    self._probed_firmware_info.firmware_version
                    if self._probed_firmware_info is not None
                    else None
                ),
            },
        )


class BaseHomeAssistantYellowOptionsFlow(OptionsFlow, ABC):
    """Base Home Assistant Yellow options flow shared between firmware and multi-PAN."""

    _hw_settings: dict[str, bool] | None = None

    def __init__(self, hass: HomeAssistant, *args: Any, **kwargs: Any) -> None:
        """Instantiate options flow."""
        super().__init__(*args, **kwargs)
        self._supervisor_client = get_supervisor_client(hass)

    @abstractmethod
    async def async_step_main_menu(self, _: None = None) -> ConfigFlowResult:
        """Show the main menu."""

    @final
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Manage the options flow."""
        return await self.async_step_main_menu()

    @final
    async def async_step_on_supervisor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle logic when on Supervisor host."""
        return await self.async_step_main_menu()

    async def async_step_hardware_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle hardware settings."""

        if user_input is not None:
            if self._hw_settings == user_input:
                return self.async_create_entry(data={})
            try:
                async with asyncio.timeout(10):
                    await async_set_yellow_settings(self.hass, user_input)
            except (aiohttp.ClientError, TimeoutError, HassioAPIError) as err:
                _LOGGER.warning("Failed to write hardware settings", exc_info=err)
                return self.async_abort(reason="write_hw_settings_error")
            return await self.async_step_reboot_menu()

        try:
            async with asyncio.timeout(10):
                self._hw_settings: dict[str, bool] = await async_get_yellow_settings(
                    self.hass
                )
        except (aiohttp.ClientError, TimeoutError, HassioAPIError) as err:
            _LOGGER.warning("Failed to read hardware settings", exc_info=err)
            return self.async_abort(reason="read_hw_settings_error")

        schema = self.add_suggested_values_to_schema(
            STEP_HW_SETTINGS_SCHEMA, self._hw_settings
        )

        return self.async_show_form(step_id="hardware_settings", data_schema=schema)

    async def async_step_reboot_menu(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm reboot host."""
        return self.async_show_menu(
            step_id="reboot_menu",
            menu_options=[
                "reboot_now",
                "reboot_later",
            ],
        )

    async def async_step_reboot_now(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reboot now."""
        await self._supervisor_client.host.reboot()
        return self.async_create_entry(data={})

    async def async_step_reboot_later(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Reboot later."""
        return self.async_create_entry(data={})


class HomeAssistantYellowMultiPanOptionsFlowHandler(
    BaseHomeAssistantYellowOptionsFlow, MultiprotocolOptionsFlowHandler
):
    """Handle a multi-PAN options flow for Home Assistant Yellow."""

    async def async_step_main_menu(self, _: None = None) -> ConfigFlowResult:
        """Show the main menu."""
        return self.async_show_menu(
            step_id="main_menu",
            menu_options=[
                "hardware_settings",
                "multipan_settings",
            ],
        )

    async def async_step_multipan_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle multipan settings."""
        return await MultiprotocolOptionsFlowHandler.async_step_on_supervisor(
            self, user_input
        )

    async def _async_serial_port_settings(
        self,
    ) -> MultiprotocolSerialPortSettings:
        """Return the radio serial port settings."""
        return MultiprotocolSerialPortSettings(
            device=RADIO_DEVICE,
            baudrate="115200",
            flow_control=True,
        )

    async def _async_zha_physical_discovery(self) -> dict[str, Any]:
        """Return ZHA discovery data when multiprotocol FW is not used.

        Passed to ZHA do determine if the ZHA config entry is connected to the radio
        being migrated.
        """
        return {"hw": ZHA_HW_DISCOVERY_DATA}

    def _zha_name(self) -> str:
        """Return the ZHA name."""
        return "Yellow Multiprotocol"

    def _hardware_name(self) -> str:
        """Return the name of the hardware."""
        return BOARD_NAME

    async def async_step_flashing_complete(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Finish flashing and update the config entry."""
        self.hass.config_entries.async_update_entry(
            entry=self.config_entry,
            data={
                **self.config_entry.data,
                FIRMWARE: ApplicationType.EZSP.value,
            },
        )

        return await super().async_step_flashing_complete(user_input)


class HomeAssistantYellowOptionsFlowHandler(
    YellowFirmwareMixin,
    BaseHomeAssistantYellowOptionsFlow,
    BaseFirmwareOptionsFlow,
):
    """Handle a firmware options flow for Home Assistant Yellow."""

    def __init__(self, hass: HomeAssistant, *args: Any, **kwargs: Any) -> None:
        """Instantiate options flow."""
        super().__init__(hass, *args, **kwargs)

        self._hardware_name = BOARD_NAME
        self._device = RADIO_DEVICE

        self._probed_firmware_info = FirmwareInfo(
            device=self._device,
            firmware_type=ApplicationType(self.config_entry.data["firmware"]),
            firmware_version=None,
            source="guess",
            owners=[],
        )

        # Regenerate the translation placeholders
        self._get_translation_placeholders()

    async def async_step_main_menu(self, _: None = None) -> ConfigFlowResult:
        """Show the main menu."""
        return self.async_show_menu(
            step_id="main_menu",
            menu_options=[
                "hardware_settings",
                "firmware_settings",
            ],
        )

    async def async_step_firmware_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle firmware configuration settings."""
        return await super().async_step_pick_firmware()

    def _async_flow_finished(self) -> ConfigFlowResult:
        """Create the config entry."""
        assert self._probed_firmware_info is not None

        self.hass.config_entries.async_update_entry(
            entry=self.config_entry,
            data={
                **self.config_entry.data,
                FIRMWARE: self._probed_firmware_info.firmware_type.value,
                FIRMWARE_VERSION: self._probed_firmware_info.firmware_version,
            },
        )

        return self.async_create_entry(title="", data={})
