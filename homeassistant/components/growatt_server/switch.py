"""Module for Growatt switch integration with Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import AUTH_API_TOKEN, CONF_AUTH_TYPE, DEFAULT_AUTH_TYPE, DOMAIN
from .coordinator import GrowattCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class GrowattRequiredNumberKey:
    """Mixin for required keys."""

    api_key: str


@dataclass(frozen=True)
class GrowattSwitchEntityDescription(SwitchEntityDescription, GrowattRequiredNumberKey):
    """Describes Growatt switch entity."""


TLX_SWITCH_TYPES: tuple[GrowattSwitchEntityDescription, ...] = (
    GrowattSwitchEntityDescription(
        api_key="ac_charge",
        key="tlx_ac_charge",
        translation_key="tlx_ac_charge",
    ),
)


class GrowattSwitch(CoordinatorEntity, SwitchEntity):
    """Representation of a Growatt switch."""

    _attr_has_entity_name = True
    _pending_state: str | None
    coordinator: GrowattCoordinator
    entity_description: GrowattSwitchEntityDescription

    def __init__(
        self,
        coordinator: GrowattCoordinator,
        device_name: str,
        serial_id: str,
        unique_id: str,
        description: GrowattSwitchEntityDescription,
    ) -> None:
        """Initialize a Growatt switch."""
        super().__init__(coordinator)
        self.entity_description = description

        self._attr_unique_id = unique_id
        self._attr_icon = "mdi:solar-power"
        self._attr_is_on = None  # Initialize to None
        self._pending_state = None  # To track state changes that haven't been confirmed
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_id)}, manufacturer="Growatt", name=device_name
        )

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if self._pending_state is not None:
            return self._pending_state == "1"

        value = self.coordinator.get_value(self.entity_description)
        if value is None:
            return None

        # Handle both string "1" and integer 1
        if isinstance(value, str):
            return value == "1"
        return bool(int(value))

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._async_set_state("1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._async_set_state("0")

    async def _async_set_state(self, state: str) -> None:
        """Set the switch state."""
        try:
            # Store the pending state before making the API call
            self._pending_state = state
            self.async_write_ha_state()  # Update UI immediately to show pending state

            # Check authentication type and use the appropriate API method
            auth_type = self.coordinator.config.get(CONF_AUTH_TYPE, DEFAULT_AUTH_TYPE)

            if auth_type == AUTH_API_TOKEN:
                # Use V1 API with token
                res = await self.hass.async_add_executor_job(
                    self.coordinator.api.min_write_parameter,
                    self.coordinator.device_id,
                    self.entity_description.api_key,
                    state,
                )
                _LOGGER.debug(
                    "Set switch using V1 API: %s to state: %s, res: %s",
                    self.entity_description.api_key,
                    state,
                    res,
                )

                if res.get("error_code", 1) == 0:
                    # Success, update the value in coordinator
                    self.coordinator.set_value(self.entity_description, state)
                    self._pending_state = None  # Clear pending state
                    self.async_write_ha_state()
                else:
                    # Failed - revert the pending state
                    self._pending_state = None
                    self.async_write_ha_state()
                    _LOGGER.error(
                        "Set switch: %s to state: %s failed with error: %s",
                        self.entity_description.key,
                        state,
                        res.get("error_msg", "Unknown error"),
                    )
                    raise HomeAssistantError(
                        f"Failed to set switch {self.entity_description.key} to state {state}: {res.get('error_msg')}"
                    )
            else:
                # Traditional API
                res = await self.hass.async_add_executor_job(
                    self.coordinator.api.update_tlx_inverter_setting,
                    self.coordinator.device_id,
                    self.entity_description.api_key,
                    state,
                )
                _LOGGER.debug(
                    "Set switch: %s to state: %s, res: %s",
                    self.entity_description.key,
                    state,
                    res,
                )
                if res.get("success"):
                    # Success - update coordinator data
                    self.coordinator.set_value(self.entity_description, state)
                    self._pending_state = None  # Clear pending state
                    self.async_write_ha_state()
                else:
                    # Failed - revert the pending state
                    self._pending_state = None
                    self.async_write_ha_state()
                    _LOGGER.error(
                        "Turn %s switch %s failed, msg: %s, error: %s",
                        "on" if state == "1" else "off",
                        self.entity_description.key,
                        res.get("msg"),
                        res.get("error"),
                    )
                    raise HomeAssistantError(
                        f"Failed to set switch {self.entity_description.key} to state {state}: {res.get('msg')}"
                    )

        except (KeyError, ValueError) as e:
            # Failed - revert the pending state
            self._pending_state = None
            self.async_write_ha_state()
            _LOGGER.error("Error while setting switch state: %s", e)
            raise HomeAssistantError(f"Error while setting switch state: {e}") from e

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Only reset pending state if we're not in the middle of an update
        if self._pending_state is None:
            self.async_write_ha_state()


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the Growatt switch."""
    coordinators = hass.data[DOMAIN][config_entry.entry_id]

    entities = []

    # Add switches for each device type
    for device_sn, device_coordinator in coordinators["devices"].items():
        if device_coordinator.device_type == "tlx":
            for description in TLX_SWITCH_TYPES:
                entities.extend(
                    [
                        GrowattSwitch(
                            device_coordinator,
                            device_name=device_sn,
                            serial_id=device_sn,
                            unique_id=f"{device_sn}-{description.key}",
                            description=description,
                        )
                    ]
                )

        else:
            _LOGGER.debug(
                "Device type %s was found but is not supported right now",
                device_coordinator.device_type,
            )

    async_add_entities(entities, True)
