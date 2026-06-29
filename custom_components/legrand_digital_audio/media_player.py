import json
import logging
import asyncio

from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)

from datetime import timedelta
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)
SOCKET_TIMEOUT = 10

ALL_KEY = "all"


async def async_setup_entry(hass, config, async_add_entities) -> None:
    """Set up the Legrand Digital Audio platform."""
    _LOGGER.debug(hass.data[DOMAIN][config.entry_id])
    entry_data = hass.data[DOMAIN][config.entry_id]
    shared_socket = entry_data["socket"]
    zones = entry_data["zones"]
    entities_registry = entry_data["entities"]

    entities = []
    zone_ids = []

    for zone in zones:
        name = zone.get("name")
        zone_id = zone.get("zone_id")
        sources = zone.get("sources")

        if not name or not zone_id:
            _LOGGER.error(f"Invalid zone configuration: {zone}")
            continue

        zone_ids.append(f"{zone_id}")
        entity = LegrandDigitalAudio(
            name, shared_socket, zone_id, sources, entities_registry, config.entry_id
        )
        entities_registry[zone_id] = entity
        entities.append(entity)

    # Aggregate "all" entity. Use the first zone's sources as the canonical list
    # (config_flow assigns the same SourceList to every zone today).
    aggregate_sources = zones[0]["sources"] if zones else []
    aggregate = LegrandDigitalAudio(
        ALL_KEY,
        shared_socket,
        zone_ids,
        aggregate_sources,
        entities_registry,
        config.entry_id,
    )
    entities_registry[ALL_KEY] = aggregate
    entities.append(aggregate)

    async_add_entities(entities)


class LegrandDigitalAudio(MediaPlayerEntity):
    """Representation of a media player controlled via a socket."""

    def __init__(
        self,
        name,
        shared_socket,
        zone_id,
        sources,
        entities_registry,
        entry_id,
    ):
        """Initialize the media player."""
        self._name = f"{name}"
        self._socket = shared_socket
        self._zone_id = zone_id
        self._state = MediaPlayerState.OFF
        self._volume = 0.05
        self._source = sources[0]["Name"] if sources else None
        self._source_list = sources or []
        self._is_muted = False
        self._lock = asyncio.Lock()
        self._entities_registry = entities_registry
        self._is_aggregate = isinstance(zone_id, list)

        if self._is_aggregate:
            self._command_id = 100000000
            # Match the legacy unique_id format so this entity re-binds to the
            # existing entity registry entry (and keeps its entity_id, e.g.
            # media_player.legrand_audio_zone_all). For fresh installs a
            # friendly object_id is suggested below.
            self._unique_id = f"{DOMAIN}_{self._zone_id}"
            self._attr_suggested_object_id = "legrand_audio_zone_all"
        else:
            self._command_id = (
                int(f"{zone_id.replace('Z', '')}00000000") + 100000000
            )
            self._unique_id = f"{DOMAIN}_{self._zone_id}"

    def _get_next_command_id(self):
        """Generate the next unique command ID."""
        self._command_id += 1
        return self._command_id

    async def _send_command(self, command):
        """Send a command to the device and wait for the response."""
        async with self._lock:
            try:
                command_data = json.loads(command)
                sent_command_id = command_data.get("ID")

                _LOGGER.debug(f"Sent: {command}")
                await asyncio.get_event_loop().sock_sendall(
                    self._socket, str(command + "\n").encode("utf-8")
                )

                try:
                    return await asyncio.wait_for(
                        self._read_response(sent_command_id),
                        timeout=SOCKET_TIMEOUT,
                    )
                except asyncio.TimeoutError:
                    # Without this guard a missed reply would hold the shared
                    # lock forever and freeze every other entity's update.
                    _LOGGER.warning(
                        f"Timeout waiting for response to command ID {sent_command_id}"
                    )
                    return None
            except Exception as e:
                _LOGGER.error(f"Socket communication error: {e}")
                return None

    async def _read_response(self, sent_command_id):
        """Read framed JSON messages from the socket until ours arrives."""
        buffer = ""
        while True:
            data = await asyncio.get_event_loop().sock_recv(self._socket, 1024)
            if not data:
                _LOGGER.error("Socket connection closed by the device.")
                return None

            buffer += data.decode("utf-8")
            messages = buffer.split("\x00")
            buffer = messages.pop()

            for message in messages:
                try:
                    response_json = json.loads(message)
                    _LOGGER.debug(f"Received: {response_json}")

                    response_id = response_json.get("ID")
                    if response_id == sent_command_id:
                        return response_json
                    else:
                        _LOGGER.warning(
                            f"Response ID {response_id} does not match sent ID {sent_command_id}. Ignoring."
                        )
                except json.JSONDecodeError:
                    _LOGGER.error(f"Failed to parse JSON: {message}")

    def _parse_response(self, response):
        """Parse the response from the device and update the state."""
        try:
            if "PropertyList" in response:
                properties = response["PropertyList"]
                self._state = (
                    MediaPlayerState.ON
                    if properties.get("Power")
                    else MediaPlayerState.OFF
                )
                self._volume = round(
                    properties.get("Volume", self._volume * 100) / 100, 2
                )
                for obj in self._source_list:
                    if obj.get("SID") == properties.get("Source"):
                        self._source = obj.get("Name")
                self._is_muted = properties.get("Muted", self._is_muted)
        except Exception as e:
            _LOGGER.error(f"Failed to parse response: {e}")

    async def async_update(self):
        """Fetch the latest state from the device."""
        if self._is_aggregate:
            await self._update_from_peers()
            return

        command_id = self._get_next_command_id()
        command = json.dumps(
            {
                "ID": command_id,
                "Service": "ReportZoneProperties",
                "ZID": self._zone_id,
            }
        )
        response = await self._send_command(command)
        if response:
            self._parse_response(response)

    def _peer_zone_entities(self):
        """Return all per-zone entities (excluding the aggregate)."""
        return [
            entity
            for key, entity in self._entities_registry.items()
            if key != ALL_KEY
        ]

    async def _update_from_peers(self):
        """Refresh each zone and aggregate their state into this entity."""
        peers = self._peer_zone_entities()
        if not peers:
            return

        # Refresh each peer sequentially through its own update path so the
        # shared socket lock is respected.
        for peer in peers:
            try:
                await peer.async_update()
            except Exception as e:
                _LOGGER.warning(f"Aggregate update: peer refresh failed: {e}")

        any_on = any(p._state == MediaPlayerState.ON for p in peers)
        self._state = MediaPlayerState.ON if any_on else MediaPlayerState.OFF

        volumes = [p._volume for p in peers if p._volume is not None]
        if volumes:
            self._volume = round(sum(volumes) / len(volumes), 2)

        # Muted only if every zone is muted.
        self._is_muted = all(p._is_muted for p in peers)

        # Source: report the common value if all peers agree, else keep prior.
        sources = {p._source for p in peers if p._source is not None}
        if len(sources) == 1:
            self._source = next(iter(sources))

    def _refresh_peers(self):
        """Schedule an immediate state refresh on related entities."""
        if self._is_aggregate:
            for peer in self._peer_zone_entities():
                peer.async_schedule_update_ha_state(force_refresh=True)
        else:
            aggregate = self._entities_registry.get(ALL_KEY)
            if aggregate is not None:
                aggregate.async_schedule_update_ha_state(force_refresh=True)

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return self._unique_id

    @property
    def name(self):
        """Return the name of the media player."""
        return self._name

    @property
    def state(self):
        """Return the state of the media player."""
        return self._state

    @property
    def volume_level(self):
        """Return the volume level (0.0 to 1.0)."""
        return self._volume

    @property
    def source(self):
        """Return the Source of the speakers, or None when powered off."""
        if self._state == MediaPlayerState.OFF:
            return None
        return self._source

    @property
    def is_volume_muted(self):
        """Return True if the volume is muted."""
        return self._is_muted

    @property
    def source_list(self):
        """Return the list of available input sources."""
        return [source["Name"] for source in self._source_list]

    @property
    def supported_features(self):
        """Return the supported features."""
        return (
            MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
            | MediaPlayerEntityFeature.SELECT_SOURCE
        )

    async def async_turn_on(self):
        """Turn the media player on."""
        command_id = self._get_next_command_id()
        command = json.dumps(
            {
                "ID": command_id,
                "Service": "SetZoneProperty",
                "ZID": self._zone_id,
                "PropertyList": {"Power": True},
            }
        )
        await self._send_command(command)
        self._state = MediaPlayerState.ON
        self.async_write_ha_state()
        self._refresh_peers()

    async def async_turn_off(self):
        """Turn the media player off."""
        command_id = self._get_next_command_id()
        command = json.dumps(
            {
                "ID": command_id,
                "Service": "SetZoneProperty",
                "ZID": self._zone_id,
                "PropertyList": {"Power": False},
            }
        )
        await self._send_command(command)
        self._state = MediaPlayerState.OFF
        self.async_write_ha_state()
        self._refresh_peers()

    async def async_set_volume_level(self, volume):
        """Set the volume level."""
        command_id = self._get_next_command_id()
        command = json.dumps(
            {
                "ID": command_id,
                "Service": "SetZoneProperty",
                "ZID": self._zone_id,
                "PropertyList": {"Volume": int(volume * 100)},
            }
        )
        await self._send_command(command)
        self._volume = volume
        self.async_write_ha_state()
        self._refresh_peers()

    async def async_mute_volume(self, mute):
        """Mute or unmute the volume."""
        command_id = self._get_next_command_id()
        command = json.dumps(
            {
                "ID": command_id,
                "Service": "SetZoneProperty",
                "ZID": self._zone_id,
                "PropertyList": {"Mute": mute},
            }
        )
        await self._send_command(command)
        self._is_muted = mute
        self.async_write_ha_state()
        self._refresh_peers()

    async def async_select_source(self, source):
        """Select an input source."""
        for obj in self._source_list:
            if obj.get("Name") == source:
                source_val = obj.get("SID")
                command_id = self._get_next_command_id()
                command = json.dumps(
                    {
                        "ID": command_id,
                        "Service": "SetZoneProperty",
                        "ZID": self._zone_id,
                        "PropertyList": {"Source": source_val},
                    }
                )
                await self._send_command(command)
                self._source = source_val
                self.async_write_ha_state()
                self._refresh_peers()
                return
