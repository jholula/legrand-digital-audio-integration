import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import Platform

from .const import (
    CONF_DEVICE_TYPE,
    DEFAULT_PORT,
    DEVICE_TYPE_AU7000,
    DEVICE_TYPE_AU7001,
    DOMAIN,
)
from .connection import LegrandConnection
from .upnp import NuvoUpnpZone

PLATFORMS = [Platform.MEDIA_PLAYER]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration using YAML (if applicable)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    # Entries created before multi-device support default to the AU7000.
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_AU7000)
    hass.data.setdefault(DOMAIN, {})

    if device_type == DEVICE_TYPE_AU7001:
        await _async_setup_au7001(hass, entry)
    else:
        await _async_setup_au7000(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def _async_setup_au7000(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up an AU7000 distribution module (TCP/JSON control)."""
    host = entry.data["host"]
    port = entry.data.get("port", DEFAULT_PORT)
    zones = entry.data["zones"]

    if not host or not port:
        raise ConfigEntryNotReady("Host or port missing from configuration entry")

    _LOGGER.info(
        "Setting up %s Legrand Digital Audio zones on %s:%s", len(zones), host, port
    )

    # Establish the shared connection (handshake included). If the device is
    # unreachable at startup, tell HA to retry setup later instead of failing.
    connection = LegrandConnection(host, port)
    try:
        await connection.async_connect()
    except Exception as e:  # noqa: BLE001
        raise ConfigEntryNotReady(
            f"Unable to connect to Legrand Digital Audio at {host}:{port}: {e}"
        ) from e

    # `entities` is populated by the media_player platform during setup so that
    # any entity (notably the aggregate "all" entity) can look up its peers and
    # trigger immediate state refreshes after issuing commands.
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_DEVICE_TYPE: DEVICE_TYPE_AU7000,
        "connection": connection,
        "zones": zones,
        "entities": {},
    }


async def _async_setup_au7001(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Set up an AU7001 streaming zone (UPnP/SOAP control)."""
    location = entry.data["location"]
    udn = entry.data["udn"]
    name = entry.data.get("name", "Legrand Digital Audio")

    _LOGGER.info("Setting up Legrand AU7001 streaming zone '%s' (%s)", name, udn)

    zone = NuvoUpnpZone(hass, location, udn, name)
    # Confirm the device is reachable so state is populated before entities load.
    await zone.async_update()
    if not zone.available:
        raise ConfigEntryNotReady(
            f"Unable to reach Legrand AU7001 zone at {location}"
        )

    hass.data[DOMAIN][entry.entry_id] = {
        CONF_DEVICE_TYPE: DEVICE_TYPE_AU7001,
        "upnp": zone,
    }


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Legrand Digital Audio config entry")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if entry_data:
        if "connection" in entry_data:
            try:
                await entry_data["connection"].async_close()
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Error closing connection: %s", e)
        elif "upnp" in entry_data:
            await entry_data["upnp"].async_close()

    return unload_ok
