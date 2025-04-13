"""Coordinator module for managing Growatt data fetching."""

import datetime
import logging
import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_TOKEN, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from . import growattServer2 as growattServer
from .const import (
    AUTH_API_TOKEN,
    CONF_AUTH_TYPE,
    DEFAULT_AUTH_TYPE,
    DEFAULT_URL,
    DEPRECATED_URLS,
    DOMAIN,
)

SCAN_INTERVAL = datetime.timedelta(minutes=5)

_LOGGER = logging.getLogger(__name__)
_LOGGER.setLevel(logging.DEBUG)


class GrowattCoordinator(DataUpdateCoordinator):
    """Coordinator to manage Growatt data fetching."""

    # Class-level variable to track last API call for each endpoint
    # Using just the method name as key to ensure rate limiting across all devices
    _last_api_calls: dict[str, float] = {}

    def _add_legacy_keys(self, data):
        """Add legacy keys for settings data."""
        if not data:
            return {}

        processed_data = data.copy()  # Create a copy to avoid modifying the original

        # Map of new keys to old keys with defaults
        key_mapping = {
            "acChargeEnable": "ac_charge",
            "chargePowerCommand": "charge_power",
            "wchargeSOCLowLimit": "charge_stop_soc",
            "disChargePowerCommand": "discharge_power",
            "wdisChargeSOCLowLimit": "discharge_stop_soc",
            "onGridDischargeStopSOC": "on_grid_discharge_stop_soc",
            "bdc1Soc": "SOC",
            "bdc1ChargePower": "chargePower",
            "bdc1DischargePower": "pdisCharge",
        }

        # Add compatibility keys
        for new_key, old_key in key_mapping.items():
            if new_key in processed_data and processed_data[new_key] not in [None, ""]:
                processed_data[old_key] = processed_data[new_key]
                _LOGGER.debug(
                    "Added legacy key: %s -> %s = %s",
                    new_key,
                    old_key,
                    processed_data[new_key],
                )

        # epvToday is missing, so we need to calculate it
        # Calculate epvToday (total solar generation today) by summing the individual PV inputs
        if "epvToday" not in processed_data and any(
            key in processed_data
            for key in ("epv1Today", "epv2Today", "epv3Today", "epv4Today")
        ):
            total_pv_today = 0.0
            for i in range(1, 5):
                pv_key = f"epv{i}Today"
                if pv_key in processed_data and processed_data[pv_key] not in (
                    None,
                    "",
                ):
                    total_pv_today += float(processed_data[pv_key])

            processed_data["epvToday"] = total_pv_today
            _LOGGER.debug(
                "Calculated epvToday = %s from sum of individual PV inputs",
                total_pv_today,
            )

        return processed_data

    @classmethod
    def can_call_endpoint(cls, endpoint_name: str, device_id=None) -> bool:
        """Check if an endpoint can be called based on rate limiting."""
        current_time = time.time()
        min_interval = 240  # 4 minutes in seconds

        # Create a unique key that includes both endpoint and device
        rate_limit_key = f"{endpoint_name}_{device_id}" if device_id else endpoint_name

        if rate_limit_key not in cls._last_api_calls:
            _LOGGER.debug(
                "Endpoint %s for device %s never called before, allowed",
                endpoint_name,
                device_id or "N/A",
            )
            return True

        time_since_last_call = current_time - cls._last_api_calls[rate_limit_key]
        if time_since_last_call >= min_interval:
            _LOGGER.debug(
                "Endpoint %s for device %s last called %.1fs ago (limit %.1fs), allowed",
                endpoint_name,
                device_id or "N/A",
                time_since_last_call,
                min_interval,
            )
            return True

        _LOGGER.debug(
            "Endpoint %s for device %s last called %.1fs ago (limit %.1fs), skipping",
            endpoint_name,
            device_id or "N/A",
            time_since_last_call,
            min_interval,
        )
        return False

    @classmethod
    def record_api_call(cls, endpoint_name: str, device_id=None) -> None:
        """Record that an API call was made to this endpoint."""
        rate_limit_key = f"{endpoint_name}_{device_id}" if device_id else endpoint_name
        cls._last_api_calls[rate_limit_key] = time.time()

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        device_id: str,
        device_type: str,
        plant_id: str,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
        self.config = {**config_entry.data}
        self.auth_type = self.config.get(CONF_AUTH_TYPE, DEFAULT_AUTH_TYPE)

        # Initialize API based on auth type
        if self.auth_type == AUTH_API_TOKEN:
            self.api = growattServer.GrowattApi(token=self.config[CONF_TOKEN])
        else:
            self.username = self.config[CONF_USERNAME]
            self.password = self.config[CONF_PASSWORD]
            self.url = self.config.get(CONF_URL, DEFAULT_URL)
            self.api = growattServer.GrowattApi(
                add_random_user_id=True, agent_identifier=self.username
            )

            # Handle URL deprecation
            if self.url in DEPRECATED_URLS:
                _LOGGER.warning(
                    "URL: %s has been deprecated, migrating to the latest default: %s",
                    self.url,
                    DEFAULT_URL,
                )
                self.url = DEFAULT_URL
                self.config[CONF_URL] = self.url
                hass.config_entries.async_update_entry(config_entry, data=self.config)

            # Set server URL for password auth
            self.api.server_url = self.url

        self.device_id = device_id
        self.device_type = device_type
        self.plant_id = plant_id
        self.data = {}
        self.previous_values: dict[str, Any] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} ({config_entry.unique_id})",
            update_method=self._async_update_data,
            update_interval=SCAN_INTERVAL,
        )

    async def _make_api_call(self, api_method, *args):
        """Make an API call with rate limiting."""
        method_name = api_method.__name__
        endpoint_key = method_name
        result = None

        # Apply rate limiting to all auth types
        if not self.can_call_endpoint(endpoint_key, self.device_id):
            _LOGGER.warning(
                "Using cached data for %s %s due to rate limiting",
                self.device_type,
                self.device_id,
            )
            return None  # Skip this call due to rate limiting

        try:
            result = await self.hass.async_add_executor_job(api_method, *args)
            # Always record API calls regardless of auth type
            self.record_api_call(endpoint_key, self.device_id)
        except ValueError as err:
            _LOGGER.debug("make_api_call failed: %s", err)

            # Token auth doesn't support re-login
            if self.auth_type == AUTH_API_TOKEN:
                # Check for rate limiting error
                if "frequently" in str(err).lower():
                    _LOGGER.warning(
                        "API rate limit reached for %s %s. Using cached data",
                        self.device_type,
                        self.device_id,
                    )
                    self.record_api_call(
                        endpoint_key, self.device_id
                    )  # Record the rate limit hit
                raise  # Re-raise the exception

            # Password auth - attempt re-login
            login_response = await self.hass.async_add_executor_job(
                self.api.login, self.username, self.password
            )
            if not login_response["success"]:
                _LOGGER.error(
                    "Failed to log in, msg: %s, error: %s",
                    login_response["msg"],
                    login_response["error"],
                )
                return None

            _LOGGER.debug("make_api_call: login success")
            try:
                result = await self.hass.async_add_executor_job(api_method, *args)
                self.record_api_call(
                    endpoint_key, self.device_id
                )  # Record successful retry
            except (ValueError, KeyError, TypeError) as e:
                # More specific exceptions instead of a blind Exception catch
                _LOGGER.error("Error after login: %s", e)
                return None

        return result

    async def _async_update_data(self) -> dict:
        """Fetch data from API endpoint."""
        try:
            _LOGGER.debug("Updating data for %s (%s)", self.device_id, self.device_type)

            if self.auth_type == AUTH_API_TOKEN and self.device_type == "tlx":
                await self._update_tlx_with_token_auth()
            elif self.auth_type == AUTH_API_TOKEN and self.device_type == "total":
                await self._update_total_with_token_auth()
            # Continue with existing code for other device types
            elif self.device_type == "total":
                await self._update_total_with_password_auth()
            elif self.device_type == "inverter":
                await self._update_inverter()
            elif self.device_type == "tlx":
                await self._update_tlx_with_password_auth()
            elif self.device_type == "storage":
                await self._update_storage()
            elif self.device_type == "mix":
                await self._update_mix()
            _LOGGER.debug(
                "Finished updating data for %s (%s)",
                self.device_id,
                self.device_type,
            )

        except KeyError as err:
            _LOGGER.error("Key error while fetching data: %s", err)
        except ValueError as err:
            _LOGGER.error("Value error while fetching data: %s", err)
        except TypeError as err:
            _LOGGER.error("Type error while fetching data: %s", err)

        return self.data or {}

    async def _update_tlx_with_token_auth(self):
        """Update data for TLX device using token authentication."""
        # Check rate limiting first
        if not self.can_call_endpoint("min_energy", self.device_id):
            _LOGGER.warning(
                "Using cached data for TLX device %s due to rate limiting",
                self.device_id,
            )
            # Don't modify self.data, just return
            return

        # Only make API call if not rate limited
        energy_response = await self._make_api_call(self.api.min_energy, self.device_id)

        # Record the API call
        self.record_api_call("min_energy", self.device_id)

        # If API call failed for some reason
        if energy_response is None or energy_response.get("error_code", 1) != 0:
            error_msg = (
                energy_response.get("error_msg", "Unknown error")
                if energy_response
                else "API call failed"
            )
            _LOGGER.warning(
                "Failed to get TLX energy data: %s. Using cached data", error_msg
            )
            return  # Keep existing data

        # Process successful response
        energy_data = energy_response.get("data", {})

        # Get settings data (less frequent updates)
        if self.can_call_endpoint("min_settings", self.device_id):
            settings_response = await self._make_api_call(
                self.api.min_settings, self.device_id
            )

            if settings_response and settings_response.get("error_code", 1) == 0:
                self.record_api_call("min_settings", self.device_id)
                settings_data = settings_response.get("data", {})

                # Extract time segments during the regular update
                time_segments = await self.hass.async_add_executor_job(
                    self.api.min_read_time_segments,
                    self.device_id,
                    settings_response,
                )

                # Store time segments in the data dictionary
                combined_data = {
                    **energy_data,
                    **settings_data,
                    "time_segments": time_segments,
                }

                # Update self.data with all information
                self.data = self._add_legacy_keys(combined_data)
            else:
                _LOGGER.warning("Failed to get TLX settings, will use cached settings")
                # Update self.data with just energy data
                self.data = self._add_legacy_keys(energy_data)

                # Preserve time segments if present in existing data
                if hasattr(self, "data") and "time_segments" in self.data:
                    self.data["time_segments"] = self.data["time_segments"]
        else:
            # Just update with energy data, but preserve settings if we have them
            # Create a new dictionary with energy data
            new_data = self._add_legacy_keys(energy_data)

            # Copy over any settings fields we might have in the existing data
            if hasattr(self, "data") and isinstance(self.data, dict):
                for key in self.data:
                    if key not in new_data and key != "time_segments":
                        new_data[key] = self.data[key]

                # Preserve time segments specifically
                if "time_segments" in self.data:
                    new_data["time_segments"] = self.data["time_segments"]

            # Update self.data
            self.data = new_data

    async def _fetch_tlx_settings_with_token_auth(self, energy_data):
        """Fetch settings data for TLX device using token authentication."""
        settings_response = await self._make_api_call(
            self.api.min_settings, self.device_id
        )

        if settings_response and settings_response.get("error_code", 1) == 0:
            settings_data = settings_response.get("data", {})

            # Extract time segments during the regular update
            time_segments = await self.hass.async_add_executor_job(
                self.api.min_read_time_segments,
                self.device_id,
                settings_response,
            )

            # Store time segments in the data dictionary
            self.data["time_segments"] = time_segments

            # Record the successful API call
            self.record_api_call("min_settings", self.device_id)

            # Combine data, apply settings, and add legacy keys in one go
            self.data = self._add_legacy_keys({**energy_data, **settings_data})
        else:
            _LOGGER.warning("Failed to get TLX settings, will use cached settings")
            # Use only energy data with legacy keys
            self.data = self._add_legacy_keys(energy_data)

    async def _update_total_with_token_auth(self):
        """Update data for Total device using token authentication."""
        plant_response = await self._make_api_call(
            self.api.plant_details_v1, self.device_id
        )

        # If rate limited, use cached data
        if plant_response is None:
            _LOGGER.warning(
                "Using cached data for plant %s due to rate limiting",
                self.device_id,
            )
            return

        if plant_response.get("error_code", 1) != 0:
            error_msg = plant_response.get("error_msg", "Unknown error")
            # Check for rate limiting error
            if "frequently" in str(error_msg).lower():
                _LOGGER.warning(
                    "API rate limit reached for plant %s. Using cached data",
                    self.device_id,
                )
                return

            _LOGGER.error("Failed to get plant details: %s", error_msg)
            return  # Return existing data on error

        plant_data = plant_response.get("data", {})

        # Convert to classic API format
        total_data = {
            "plantMoneyText": plant_data.get("today_income", "0"),
            "currency": plant_data.get("currency", ""),
            "todayEnergy": plant_data.get("today_energy", 0),
            "totalEnergy": plant_data.get("total_energy", 0),
            "invTodayPpv": plant_data.get("current_power", 0),
            "nominalPower": plant_data.get("nominal_power", 0),
            "totalMoneyText": plant_data.get("total_income", "0"),
        }

        self.data = total_data

    async def _update_total_with_password_auth(self):
        """Update data for Total device using password authentication."""
        total_info = await self._make_api_call(self.api.plant_info, self.device_id)

        if total_info is None:
            return

        del total_info["deviceList"]
        plant_money_text, currency = total_info["plantMoneyText"].split("/")
        total_info["plantMoneyText"] = plant_money_text
        total_info["currency"] = currency
        self.data = total_info

    async def _update_inverter(self):
        """Update data for Inverter device."""
        inverter_info = await self._make_api_call(
            self.api.inverter_detail, self.device_id
        )

        if inverter_info is None:
            return

        self.data = inverter_info

    async def _update_tlx_with_password_auth(self):
        """Update data for TLX device using password authentication."""
        tlx_system_status = await self._make_api_call(
            self.api.tlx_system_status, self.plant_id, self.device_id
        )

        if tlx_system_status is None:
            return

        # the following values are returned in kW, but we want them in W
        tlx_system_status["chargePower"] = (
            float(tlx_system_status["chargePower"]) * 1000
        )
        tlx_system_status["pdisCharge"] = float(tlx_system_status["pdisCharge"]) * 1000
        tlx_energy_overview = await self._make_api_call(
            self.api.tlx_energy_overview, self.plant_id, self.device_id
        )

        if tlx_energy_overview is None:
            return

        tlx_details = await self._make_api_call(self.api.tlx_detail, self.device_id)

        if tlx_details is None:
            return

        all_settings = await self._make_api_call(
            self.api.tlx_all_settings, self.device_id
        )

        if all_settings is None:
            return

        enabled_settings = await self._make_api_call(
            self.api.tlx_enabled_settings, self.device_id
        )

        if enabled_settings is None:
            return

        # Present in web UI, but not returned in enabled_settings for some reason
        # The shinePhone UI only shows one value and probably updates bot discharge_stop_soc and on_grid_discharge_stop_soc
        enabled_settings["enable"]["on_grid_discharge_stop_soc"] = "1"
        enabled_keys = enabled_settings["enable"].keys()
        tlx_settings = {k: v for k, v in all_settings.items() if k in enabled_keys}
        self.data = {
            **tlx_system_status,
            **tlx_energy_overview,
            **tlx_details["data"],
            **tlx_settings,
        }

        await self._extract_tlx_time_segments(all_settings)

    async def _extract_tlx_time_segments(self, all_settings):
        """Extract time segments for TLX device from all settings."""
        try:
            time_segments = await self.hass.async_add_executor_job(
                self.api.min_read_time_segments,
                self.device_id,
                {"data": all_settings},
            )
            self.data["time_segments"] = time_segments
        except KeyError as err:
            _LOGGER.warning("Key error while extracting time segments: %s", err)
        except ValueError as err:
            _LOGGER.warning("Value error while extracting time segments: %s", err)
        except Exception as err:
            _LOGGER.error("Unexpected error while extracting time segments: %s", err)
            raise  # Re-raise unexpected exceptions to avoid silent failures
        finally:
            # Preserve previous time segments if extraction fails
            if "time_segments" not in self.data and hasattr(self, "data"):
                self.data["time_segments"] = self.data.get("time_segments", [])

    async def _update_storage(self):
        """Update data for Storage device."""
        storage_info_detail = await self._make_api_call(
            self.api.storage_params, self.device_id
        )

        if storage_info_detail is None:
            return

        storage_energy_overview = await self._make_api_call(
            self.api.storage_energy_overview, self.plant_id, self.device_id
        )

        if storage_energy_overview is None:
            return

        self.data = {
            **storage_info_detail["storageDetailBean"],
            **storage_energy_overview,
        }

    async def _update_mix(self):
        """Update data for Mix device."""
        mix_info = await self._make_api_call(self.api.mix_info, self.device_id)

        if mix_info is None:
            return

        mix_totals = await self._make_api_call(
            self.api.mix_totals, self.device_id, self.plant_id
        )

        if mix_totals is None:
            return

        mix_system_status = await self._make_api_call(
            self.api.mix_system_status, self.device_id, self.plant_id
        )

        if mix_system_status is None:
            return

        mix_detail = await self._make_api_call(
            self.api.mix_detail, self.device_id, self.plant_id
        )

        if mix_detail is None:
            return

        # Calculate mix detail data
        await self._calculate_mix_details(mix_detail)

        self.data = {
            **mix_info,
            **mix_totals,
            **mix_system_status,
            **mix_detail,
        }

    async def _calculate_mix_details(self, mix_detail):
        """Calculate additional details for Mix device."""
        # Get the chart data and work out the time of the last entry, use this
        # as the last time data was published to the Growatt Server
        mix_chart_entries = mix_detail["chartData"]
        sorted_keys = sorted(mix_chart_entries)

        # Create datetime from the latest entry
        date_now = dt_util.now().date()
        last_updated_time = dt_util.parse_time(str(sorted_keys[-1]))
        if last_updated_time is not None:
            mix_detail["lastdataupdate"] = datetime.datetime.combine(
                date_now, last_updated_time, dt_util.DEFAULT_TIME_ZONE
            )

        # Calculate pac_to_user_today
        pac_to_user_today = 0.0
        hour_secs = datetime.timedelta(hours=1).total_seconds()
        previous_time_val = datetime.time(0, 0, 0)  # Start at midnight
        for key in sorted_keys:
            time_val = datetime.datetime.strptime(key, "%H:%M").time()
            # Calculate the difference between this and the previous timestamp
            # to determine how long this rate has been used for
            timediff_secs = (
                datetime.datetime.combine(datetime.date.min, time_val)
                - datetime.datetime.combine(datetime.date.min, previous_time_val)
            ).total_seconds()
            multiplier = timediff_secs / hour_secs
            data_points = mix_chart_entries[key]
            pac_to_user_today += float(data_points["pacToUser"]) * multiplier
            previous_time_val = time_val

        mix_detail["etouser_combined"] = round(pac_to_user_today, 2)

    def get_currency(self):
        """Get the currency."""
        return self.data.get("currency")

    def get_data(self, entity_description):
        """Get the data."""
        # Ensure self.data is never None
        if self.data is None:
            self.data = {}

        variable = entity_description.api_key
        api_value = self.data.get(variable)
        previous_value = self.previous_values.get(variable)
        return_value = api_value

        # If we have a 'drop threshold' specified, then check it and correct if needed
        if (
            entity_description.previous_value_drop_threshold is not None
            and previous_value is not None
            and api_value is not None
        ):
            _LOGGER.debug(
                (
                    "%s - Drop threshold specified (%s), checking for drop... API"
                    " Value: %s, Previous Value: %s"
                ),
                entity_description.name,
                entity_description.previous_value_drop_threshold,
                api_value,
                previous_value,
            )
            diff = float(api_value) - float(previous_value)

            # Check if the value has dropped (negative value i.e. < 0) and it has only
            # dropped by a small amount, if so, use the previous value.
            # Note - The energy dashboard takes care of drops within 10%
            # of the current value, however if the value is low e.g. 0.2
            # and drops by 0.1 it classes as a reset.
            if -(entity_description.previous_value_drop_threshold) <= diff < 0:
                _LOGGER.debug(
                    (
                        "Diff is negative, but only by a small amount therefore not a"
                        " nightly reset, using previous value (%s) instead of api value"
                        " (%s)"
                    ),
                    previous_value,
                    api_value,
                )
                return_value = previous_value
            else:
                _LOGGER.debug(
                    "%s - No drop detected, using API value", entity_description.name
                )

        # Lifetime total values should always be increasing, they will never reset,
        # however the API sometimes returns 0 values when the clock turns to 00:00
        # local time in that scenario we should just return the previous value
        # Scenarios:
        # 1 - System has a genuine 0 value when it it first commissioned:
        #        - will return 0 until a non-zero value is registered
        # 2 - System has been running fine but temporarily resets to 0 briefly
        #     at midnight:
        #        - will return the previous value
        # 3 - HA is restarted during the midnight 'outage' - Not handled:
        #        - Previous value will not exist meaning 0 will be returned
        #        - This is an edge case that would be better handled by looking
        #          up the previous value of the entity from the recorder
        if entity_description.never_resets and api_value == 0 and previous_value:
            _LOGGER.debug(
                (
                    "API value is 0, but this value should never reset, returning"
                    " previous value (%s) instead"
                ),
                previous_value,
            )
            return_value = previous_value

        self.previous_values[variable] = return_value

        _LOGGER.debug(
            "Data request for: %s: res=%s by %s",
            entity_description.key,
            str(return_value),
            self.device_id,
        )

        return return_value

    def get_value(self, entity_description):
        """Get the raw parameter value."""
        # Ensure self.data is never None
        if self.data is None:
            self.data = {}

        return_value = self.data.get(entity_description.api_key)
        _LOGGER.debug(
            "Get parameter value for: %s, res=%s, by %s",
            entity_description.key,
            str(return_value),
            self.device_id,
        )
        return return_value

    def set_value(self, entity_description, value):
        """Set value of the parameter."""
        # Ensure self.data is never None
        if self.data is None:
            self.data = {}

        _LOGGER.debug(
            "Set parameter value %s for: %s",
            value,
            entity_description.key,
        )
        self.data[entity_description.api_key] = value

    async def update_tlx_inverter_time_segment(
        self, segment_id, batt_mode, start_time, end_time, enabled
    ):
        """Update a TLX inverter time segment."""
        _LOGGER.debug(
            "Updating TLX inverter time segment %s for serial number %s",
            segment_id,
            self.device_id,
        )

        if self.auth_type == AUTH_API_TOKEN:
            # Use V1 API for token authentication
            response = await self.hass.async_add_executor_job(
                self.api.min_write_time_segment,
                self.device_id,
                segment_id,
                batt_mode,
                start_time,
                end_time,
                enabled,
            )
            if response.get("error_code", 1) == 0:
                _LOGGER.info(
                    "Successfully updated TLX inverter time segment %s for serial number %s",
                    segment_id,
                    self.device_id,
                )
            else:
                _LOGGER.error(
                    "Failed to update TLX inverter time segment %s for serial number %s: %s",
                    segment_id,
                    self.device_id,
                    response.get("error_msg", "Unknown error"),
                )
        else:
            # Use classic API for username/password authentication
            response = await self.hass.async_add_executor_job(
                self.api.update_tlx_inverter_time_segment,
                self.device_id,
                segment_id,
                batt_mode,
                start_time,
                end_time,
                enabled,
            )
            if response.get("success"):
                _LOGGER.info(
                    "Successfully updated TLX inverter time segment %s for serial number %s",
                    segment_id,
                    self.device_id,
                )
            else:
                _LOGGER.error(
                    "Failed to update TLX inverter time segment %s for serial number %s: %s",
                    segment_id,
                    self.device_id,
                    response.get("msg"),
                )

    async def read_tlx_inverter_time_segments(self):
        """Read time segments from a TLX/MIN inverter."""
        _LOGGER.debug(
            "Reading TLX inverter time segments for serial number %s",
            self.device_id,
        )

        if self.auth_type != AUTH_API_TOKEN:
            _LOGGER.info("Fetching TLX inverter time segments not supported")
            return None

        if not self.data:
            _LOGGER.info("Triggering refresh to get time segments")
            await self.async_refresh()

        time_segments = []
        try:
            mode_names = {0: "Load First", 1: "Battery First", 2: "Grid First"}

            # Extract from self.data
            for i in range(1, 10):  # Segments 1-9
                # Get raw time values
                start_time_raw = self.data.get(f"forcedTimeStart{i}", "0:0")
                end_time_raw = self.data.get(f"forcedTimeStop{i}", "0:0")

                # Handle 'null' string values
                if start_time_raw == "null" or not start_time_raw:
                    start_time_raw = "0:0"
                if end_time_raw == "null" or not end_time_raw:
                    end_time_raw = "0:0"

                # Format times with leading zeros (HH:MM)
                try:
                    start_parts = start_time_raw.split(":")
                    start_hour = int(start_parts[0])
                    start_min = int(start_parts[1])
                    start_time = f"{start_hour:02d}:{start_min:02d}"
                except (ValueError, IndexError):
                    start_time = "00:00"

                try:
                    end_parts = end_time_raw.split(":")
                    end_hour = int(end_parts[0])
                    end_min = int(end_parts[1])
                    end_time = f"{end_hour:02d}:{end_min:02d}"
                except (ValueError, IndexError):
                    end_time = "00:00"

                # Get the mode value safely
                mode_raw = self.data.get(f"time{i}Mode")
                if mode_raw == "null" or mode_raw is None:
                    batt_mode = None
                else:
                    try:
                        batt_mode = int(mode_raw)
                    except (ValueError, TypeError):
                        batt_mode = None

                # Get the enabled status safely
                enabled_raw = self.data.get(f"forcedStopSwitch{i}", 0)
                if enabled_raw == "null" or enabled_raw is None:
                    enabled = False
                else:
                    try:
                        enabled = int(enabled_raw) == 1
                    except (ValueError, TypeError):
                        enabled = False

                segment = {
                    "segment_id": i,
                    "batt_mode": batt_mode,
                    "mode_name": mode_names.get(batt_mode, "Unknown"),
                    "start_time": start_time,
                    "end_time": end_time,
                    "enabled": enabled,
                }

                time_segments.append(segment)
                _LOGGER.info("TLX inverter time segment %s: %s", i, str(segment))
        except Exception as err:
            _LOGGER.error("Error reading TLX inverter time segments: %s", err)
            raise HomeAssistantError(
                f"Error reading TLX inverter time segments: {err}"
            ) from err
        else:
            return time_segments
