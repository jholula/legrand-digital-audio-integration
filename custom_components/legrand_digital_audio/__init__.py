import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import Platform

from .const import DOMAIN, DEFAULT_PORT
from .connection import LegrandConnection

PLATFORMS = [Platform.MEDIA_PLAYER]
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration using YAML (if applicable)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    _LOGGER.info("Setting up Legrand Digital Audio from config entry")

    host = entry.data["host"]
    port = entry.data.get("port", DEFAULT_PORT)
    zones = entry.data["zones"]

    if not host or not port:
        _LOGGER.error("Host or port not provided in configuration entry")
        return False

    _LOGGER.info(
        "Setting up %s Legrand Digital Audio zones on %s:%s",
        len(zones),
        host,
        port,
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
    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        "connection": connection,
        "zones": zones,
        "entities": {},
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Legrand Digital Audio config entry")

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    entry_data = hass.data[DOMAIN].pop(entry.entry_id, None)
    if entry_data and "connection" in entry_data:
        try:
            await entry_data["connection"].async_close()
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Error closing connection: %s", e)

    return unload_ok
