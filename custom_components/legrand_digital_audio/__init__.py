import logging
from urllib.parse import urlsplit

from homeassistant.components import ssdp
from homeassistant.components.ssdp import SsdpChange
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType
from homeassistant.const import Platform

from .const import (
    CONF_DEVICE_TYPE,
    DEFAULT_DEVICE_NAME_AU7000,
    DEFAULT_DEVICE_NAME_AU7001,
    DEFAULT_ENTRY_TITLE_AU7000,
    DEFAULT_ENTRY_TITLE_AU7001,
    DEFAULT_PORT,
    DEVICE_TYPE_AU7000,
    DEVICE_TYPE_AU7001,
    DOMAIN,
    NUVO_ZONE_DEVICE_TYPE,
)
from .connection import LegrandConnection
from .upnp import NuvoUpnpZone

PLATFORMS = [Platform.MEDIA_PLAYER, Platform.BUTTON]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the integration (config entries only)."""
    hass.data.setdefault(DOMAIN, {})
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the integration from a config entry."""
    # Entries created before multi-device support default to the AU7000.
    device_type = entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_AU7000)
    hass.data.setdefault(DOMAIN, {})

    _async_migrate_entry_names(hass, entry, device_type)

    if device_type == DEVICE_TYPE_AU7001:
        await _async_setup_au7001(hass, entry)
    else:
        await _async_setup_au7000(hass, entry)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


def _async_migrate_entry_names(
    hass: HomeAssistant, entry: ConfigEntry, device_type: str
) -> None:
    """Update config entry title/name left over from SSDP or older releases."""
    if device_type == DEVICE_TYPE_AU7001:
        desired_title = DEFAULT_ENTRY_TITLE_AU7001
        desired_name = DEFAULT_DEVICE_NAME_AU7001
        legacy_titles = {
            "Legrand Digital Audio (AU7001)",
            "Legrand Streaming Module (AU7001)",
        }
    else:
        desired_title = DEFAULT_ENTRY_TITLE_AU7000
        desired_name = DEFAULT_DEVICE_NAME_AU7000
        legacy_titles = {
            "Legrand Digital Audio",
            "Legrand Digital Audio (AU7000)",
        }

    new_data = dict(entry.data)
    data_changed = False

    # Older AU7001 entries stored the raw SSDP friendlyName in "name".
    current_name = new_data.get("name")
    if device_type == DEVICE_TYPE_AU7001 and (
        not current_name or str(current_name).startswith("NuVo Zone")
    ):
        if current_name:
            new_data.setdefault("ssdp_friendly_name", current_name)
        new_data["name"] = desired_name
        data_changed = True

    title_changed = False
    if entry.title != desired_title and (
        entry.title in legacy_titles or entry.title.startswith("NuVo Zone")
    ):
        title_changed = True

    if not title_changed and not data_changed:
        return

    hass.config_entries.async_update_entry(
        entry,
        title=desired_title if title_changed else entry.title,
        data=new_data if data_changed else entry.data,
    )


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
    name = entry.data.get("name", DEFAULT_DEVICE_NAME_AU7001)

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

    # Music Assistant stream start often resets the AU7001 UPnP stack onto a new
    # HTTP control port. Keep the in-memory location fresh from SSDP alives
    # without calling async_update_entry (which would reload the integration).
    async def _async_ssdp_updated(discovery_info, change: SsdpChange) -> None:
        if change == SsdpChange.BYEBYE:
            return
        new_location = discovery_info.ssdp_location
        if not new_location:
            return
        target = (udn or "").lower()
        discovered = (discovery_info.ssdp_udn or "").lower()
        usn = (discovery_info.ssdp_usn or "").lower()
        if target and discovered != target and target not in usn:
            if urlsplit(new_location).hostname != zone.host:
                return
        zone.update_location(new_location)

    entry.async_on_unload(
        await ssdp.async_register_callback(
            hass,
            _async_ssdp_updated,
            {"st": NUVO_ZONE_DEVICE_TYPE},
        )
    )


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info("Unloading Legrand Digital Audio config entry")

    # Cancel in-flight UPnP/TCP work before platform unload so polls cannot
    # hold the entry in unload_in_progress.
    entry_data = hass.data[DOMAIN].get(entry.entry_id)
    if entry_data:
        if "upnp" in entry_data:
            await entry_data["upnp"].async_close()
        elif "connection" in entry_data:
            try:
                await entry_data["connection"].async_close()
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Error closing connection: %s", e)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
