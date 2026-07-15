"""Button entities for Legrand Digital Audio."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.helpers.device_registry import CONNECTION_UPNP
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_DEVICE_TYPE,
    DEFAULT_DEVICE_NAME_AU7001,
    DEVICE_TYPE_AU7001,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config, async_add_entities) -> None:
    """Set up bind helper buttons for AU7001 entries."""
    entry_data = hass.data[DOMAIN][config.entry_id]
    if entry_data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_AU7001:
        return
    async_add_entities([LegrandStartBindButton(entry_data["upnp"], config.entry_id)])


class LegrandStartBindButton(ButtonEntity):
    """Run the app's SystemCreate step, then prompt for the physical bind button."""

    _attr_has_entity_name = True
    _attr_name = "Start bind"
    _attr_icon = "mdi:link-variant-plus"

    def __init__(self, zone, entry_id: str) -> None:
        self._zone = zone
        self._entry_id = entry_id
        self._attr_unique_id = f"{DOMAIN}_{zone.udn}_start_bind"
        connections = {(CONNECTION_UPNP, zone.udn)} if zone.udn else set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, zone.udn)},
            name=DEFAULT_DEVICE_NAME_AU7001,
            manufacturer="Legrand / NuVo",
            model="AU7001",
            configuration_url=zone.configuration_url,
            connections=connections,
        )

    @property
    def available(self) -> bool:
        return self._zone.available

    async def async_press(self) -> None:
        """Create a SystemID (software half of bind)."""
        result = await self._zone.async_attempt_bind()
        _LOGGER.info(
            "Start bind on %s → %s (SystemID=%s): %s",
            self._zone.name,
            result.get("status"),
            result.get("system_id"),
            result.get("message"),
        )
