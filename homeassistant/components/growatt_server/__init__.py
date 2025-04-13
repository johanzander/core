"""The Growatt server PV inverter sensor integration."""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, SupportsResponse
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError

from . import growattServer2 as growattServer
from .const import (
    AUTH_API_TOKEN,
    BATT_MODE_MAP,
    CONF_AUTH_TYPE,
    CONF_PLANT_ID,
    DEFAULT_AUTH_TYPE,
    DEFAULT_PLANT_ID,
    DEFAULT_URL,
    DEPRECATED_URLS,
    DOMAIN,
    LOGIN_INVALID_AUTH_CODE,
    PLATFORMS,
)
from .coordinator import GrowattCoordinator

_LOGGER = logging.getLogger(__name__)


def get_device_list(api, config):
    """Retrieve the device list for the selected plant."""
    plant_id = config[CONF_PLANT_ID]
    auth_type = config.get(CONF_AUTH_TYPE, DEFAULT_AUTH_TYPE)

    if auth_type == AUTH_API_TOKEN:
        # Using token authentication
        plant_response = api.plant_list_v1()
        if plant_response.get("error_code", 1) != 0:
            raise ConfigEntryError(
                f"Failed to get plant list: {plant_response.get('error_msg', 'Unknown error')}"
            )

        plants = plant_response.get("data", {}).get("plants", [])
        if not plants:
            raise ConfigEntryError("No plants found with provided token")

        if plant_id == DEFAULT_PLANT_ID:
            plant_id = str(plants[0].get("plant_id", ""))

        # Get device list using V1 API
        devices_response = api.device_list_v1(plant_id)
        if devices_response.get("error_code", 1) != 0:
            raise ConfigEntryError(
                f"Failed to get devices: {devices_response.get('error_msg', 'Unknown error')}"
            )

        devices = devices_response.get("data", {}).get("devices", [])

        # Convert V1 API format to match classic API
        formatted_devices = []
        for device in devices:
            device_type = device.get("type")
            device_sn = device.get("device_sn", "")

            # Map device types
            if device_type == 7:  # MIN/TLX
                device_type_str = "tlx"
            elif device_type == 1:  # Regular inverter
                device_type_str = "inverter"
            elif device_type == 2:  # Storage
                device_type_str = "storage"
            elif device_type == 8:  # PCS
                device_type_str = "mix"
            else:
                _LOGGER.warning(
                    "Device %s with type %s not fully supported", device_sn, device_type
                )
                continue

            formatted_device = {
                "deviceSn": device_sn,
                "deviceType": device_type_str,
            }
            formatted_devices.append(formatted_device)

        return [formatted_devices, plant_id]
    # Original username/password authentication
    login_response = api.login(config[CONF_USERNAME], config[CONF_PASSWORD])
    if (
        not login_response["success"]
        and login_response["msg"] == LOGIN_INVALID_AUTH_CODE
    ):
        raise ConfigEntryError("Username, Password or URL may be incorrect!")
    user_id = login_response["user"]["id"]
    if plant_id == DEFAULT_PLANT_ID:
        plant_info = api.plant_list(user_id)
        plant_id = plant_info["data"][0]["plantId"]

    # Get a list of devices for specified plant to add sensors for.
    devices = api.device_list(plant_id)
    return [devices, plant_id]


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up Growatt from a config entry."""
    config = {**config_entry.data}
    auth_type = config.get(CONF_AUTH_TYPE, DEFAULT_AUTH_TYPE)

    # Initialize the API based on auth type
    if auth_type == AUTH_API_TOKEN:
        # Token-based authentication
        token = config[CONF_TOKEN]
        api = growattServer.GrowattApi(token=token)
        _LOGGER.info("Using token-based authentication")
    else:
        # Traditional username/password authentication
        username = config[CONF_USERNAME]
        url = config.get(CONF_URL, DEFAULT_URL)
        _LOGGER.info("Usinglegacy username/password authentication")

        # If the URL has been deprecated then change to the default instead
        if url in DEPRECATED_URLS:
            _LOGGER.warning(
                "URL: %s has been deprecated, migrating to the latest default: %s",
                url,
                DEFAULT_URL,
            )
            url = DEFAULT_URL
            config[CONF_URL] = url
            hass.config_entries.async_update_entry(config_entry, data=config)

        # Initialize the Growatt API
        api = await hass.async_add_executor_job(
            growattServer.GrowattApi, True, username
        )
        api.server_url = url

    # Get device list and plant ID
    try:
        devices, plant_id = await hass.async_add_executor_job(
            get_device_list, api, config
        )
    except Exception as err:
        _LOGGER.error("Error getting device list: %s", err)
        raise ConfigEntryError(f"Failed to get device list: {err}") from err

    # Create a coordinator for the total sensors
    total_coordinator = GrowattCoordinator(
        hass, config_entry, plant_id, "total", plant_id
    )

    # Initialize with empty data to prevent errors
    total_coordinator.data = {}

    # Try to do the initial refresh, but handle errors gracefully
    await total_coordinator.async_config_entry_first_refresh()

    # Store the coordinator in hass.data
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][config_entry.entry_id] = {
        "total": total_coordinator,
        "devices": {},
    }

    # Create a coordinator for each supported device type, but stagger the refreshes
    for _i, device in enumerate(devices):
        device_type = device["deviceType"]
        if device_type not in ["inverter", "tlx", "storage", "mix"]:
            continue

        device_coordinator = GrowattCoordinator(
            hass, config_entry, device["deviceSn"], device["deviceType"], plant_id
        )

        # Initialize with empty data to prevent errors
        device_coordinator.data = {}

        # Store coordinator first before refresh
        hass.data[DOMAIN][config_entry.entry_id]["devices"][device["deviceSn"]] = (
            device_coordinator
        )

        # Set up any services needed for this device type
        if device_type == "tlx":
            # Define service handler function
            async def handle_update_tlx_inverter_time_segment(
                call, device_coordinator=device_coordinator
            ):
                segment_id = call.data["segment_id"]
                batt_mode_str = str(call.data["batt_mode"])
                start_time_str = call.data["start_time"]
                end_time_str = call.data["end_time"]
                enabled = call.data["enabled"]

                if not (1 <= segment_id <= 9):
                    raise HomeAssistantError("segment_id must be between 1 and 9")

                _LOGGER.debug(
                    "handle_update_tlx_inverter_time_segment, segment_id: %d, batt_mode: %s, start_time: %s, end_time: %s, enabled: %s",
                    segment_id,
                    batt_mode_str,
                    start_time_str,
                    end_time_str,
                    enabled,
                )

                # Convert batt_mode to the corresponding constant
                batt_mode = BATT_MODE_MAP.get(batt_mode_str)
                if batt_mode is None:
                    _LOGGER.error("Invalid battery mode: %s", batt_mode_str)
                    raise HomeAssistantError(f"Invalid battery mode: {batt_mode_str}")

                try:
                    # Convert start_time and end_time to datetime.time objects
                    start_time = datetime.strptime(start_time_str, "%H:%M").time()
                    end_time = datetime.strptime(end_time_str, "%H:%M").time()
                except ValueError:
                    _LOGGER.error("Start_time and end_time must in HH:MM format")
                    raise HomeAssistantError(
                        "start_time and end_time must be in HH:MM format"
                    ) from None

                if not isinstance(enabled, bool):
                    raise HomeAssistantError(
                        "enabled must be a boolean value (True or False)"
                    )

                try:
                    await device_coordinator.update_tlx_inverter_time_segment(
                        segment_id,
                        batt_mode,
                        start_time,
                        end_time,
                        enabled,
                    )
                except Exception as err:  # noqa: BLE001
                    _LOGGER.error(
                        "Error updating TLX inverter time segment %d: %s",
                        segment_id,
                        err,
                    )
                    raise HomeAssistantError(
                        f"Error updating TLX inverter time segment {segment_id}: {err}"
                    ) from None

            # Register the service
            hass.services.async_register(
                DOMAIN,
                "update_tlx_inverter_time_segment",
                handle_update_tlx_inverter_time_segment,
            )

            async def handle_read_tlx_inverter_time_segments(
                call, device_coordinator=device_coordinator
            ):
                """Handle read TLX inverter time segments service call."""
                # Fetch the time segments
                time_segments = (
                    await device_coordinator.read_tlx_inverter_time_segments()
                )

                # Return the response
                return {"time_segments": time_segments}

            # Register the service
            hass.services.async_register(
                DOMAIN,
                "read_tlx_inverter_time_segments",
                handle_read_tlx_inverter_time_segments,
                supports_response=SupportsResponse.ONLY,
            )

    # Set up all the entities first
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Now refresh each device coordinator one by one with delays
    for device_coordinator in hass.data[DOMAIN][config_entry.entry_id][
        "devices"
    ].values():
        await device_coordinator.async_refresh()

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    return await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
