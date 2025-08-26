import logging
import asyncio
import socket
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.typing import ConfigType
from .const import DOMAIN, SOCKET_TIMEOUT
from homeassistant.const import Platform

PLATFORMS = [Platform.MEDIA_PLAYER]
_LOGGER = logging.getLogger(__name__)

async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration using YAML (if applicable)."""
    # This is only needed if you want to support YAML configuration
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    _LOGGER.info("Setting up Legrand Digital Audio from config entry")

    # Retrieve configuration data from the entry
    host = entry.data["host"]
    port = entry.data["port"]
    zones = entry.data["zones"]  # Retrieve zones from the config entry

    if not host or not port:
        _LOGGER.error("Host or port not provided in configuration entry")
        return False

    _LOGGER.info(f"Setting up {len(zones)} Legrand Digital Media zones on {host}:{port}")

    # Create a shared socket connection
    try:
        shared_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        shared_socket.settimeout(SOCKET_TIMEOUT)
        await asyncio.get_event_loop().sock_connect(shared_socket, (host, port))
        _LOGGER.info(f"Connected to {host}:{port}")
    except Exception as e:
        _LOGGER.error(f"Failed to connect to {host}:{port}: {e}")
        return False

    # Store the shared socket and zones in Home Assistant's data dictionary
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "socket": shared_socket,
        "zones": zones,
    }

    # Forward the entry to the media_player platform
    hass.async_create_task(
        hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    )

    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Legrand Digital Audio config entry")

    # Close the shared socket connection
    entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if entry_data and "socket" in entry_data:
        try:
            entry_data["socket"].close()
            _LOGGER.info("Closed socket connection")
        except Exception as e:
            _LOGGER.error(f"Error closing socket: {e}")

    # Unload the media_player platform
    await hass.config_entries.async_forward_entry_unload(entry, PLATFORMS)

    return True