"""Define constants for the Growatt Server component."""

from homeassistant.const import Platform

from . import growattServer2 as growattServer

CONF_PLANT_ID = "plant_id"

DEFAULT_PLANT_ID = "0"

DEFAULT_NAME = "Growatt"

SERVER_URLS = [
    "https://openapi.growatt.com/",  # Other regional server
    "https://openapi-cn.growatt.com/",  # Chinese server
    "https://openapi-us.growatt.com/",  # North American server
    "https://openapi-au.growatt.com/",  # Australia Server
    "http://server.smten.com/",  # smten server
]

DEPRECATED_URLS = [
    "https://server.growatt.com/",
    "https://server-api.growatt.com/",
    "https://server-us.growatt.com/",
]

DEFAULT_URL = SERVER_URLS[0]

DOMAIN = "growatt_server"

PLATFORMS: list[Platform] = [Platform.NUMBER, Platform.SENSOR, Platform.SWITCH]

# Authentication types
AUTH_PASSWORD = "password"
AUTH_API_TOKEN = "api_token"
CONF_AUTH_TYPE = "auth_type"
DEFAULT_AUTH_TYPE = AUTH_PASSWORD

LOGIN_INVALID_AUTH_CODE = "502"

BATT_MODE_MAP = {
    "load-first": growattServer.BATT_MODE_LOAD_FIRST,
    "0": growattServer.BATT_MODE_LOAD_FIRST,
    "battery-first": growattServer.BATT_MODE_BATTERY_FIRST,
    "1": growattServer.BATT_MODE_BATTERY_FIRST,
    "grid-first": growattServer.BATT_MODE_GRID_FIRST,
    "2": growattServer.BATT_MODE_GRID_FIRST,
}
