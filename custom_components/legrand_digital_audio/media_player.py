import json
import logging
from datetime import timedelta

from homeassistant.components.media_player import (
    BrowseError,
    BrowseMedia,
    MediaClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
    MediaType,
)
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import CONNECTION_UPNP
from homeassistant.helpers.entity import DeviceInfo

from .const import (
    CONF_DEVICE_TYPE,
    DEFAULT_DEVICE_NAME_AU7000,
    DEFAULT_DEVICE_NAME_AU7001,
    DEVICE_TYPE_AU7000,
    DEVICE_TYPE_AU7001,
    DOMAIN,
    NUVO_BROWSE_ROOT,
    SERVICE_ATTEMPT_BIND,
)

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=10)

ALL_KEY = "all"


def _stream_image_url(metadata: dict, extra: dict) -> str | None:
    """Pick an HTTP(S) art URL from Music Assistant / Cast-style extras."""
    for key in ("imageUrl", "image_url", "albumArtURI", "thumb"):
        value = metadata.get(key) or extra.get(key)
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            return value
    images = metadata.get("images")
    if isinstance(images, list):
        for entry in images:
            if isinstance(entry, dict):
                url = entry.get("url")
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
            elif isinstance(entry, str) and entry.startswith(("http://", "https://")):
                return entry
    return None


def _meta_text(value) -> str | None:
    """Normalize Music Assistant metadata fields (str or list) to a string."""
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        parts = [str(part).strip() for part in value if part]
        return ", ".join(parts) if parts else None
    text = str(value).strip()
    return text or None


def _au7000_device_info(device_id: str) -> DeviceInfo:
    """Device registry entry for the AU7000 distribution module."""
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=DEFAULT_DEVICE_NAME_AU7000,
        manufacturer="Legrand / NuVo",
        model="AU7000",
    )


async def async_setup_entry(hass, config, async_add_entities) -> None:
    """Set up the Legrand Digital Audio platform."""
    _LOGGER.debug("Setting up media_player entities for entry %s", config.entry_id)
    entry_data = hass.data[DOMAIN][config.entry_id]

    if entry_data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_AU7001:
        async_add_entities([LegrandNuvoZone(entry_data["upnp"], config.entry_id)])
        platform = entity_platform.async_get_current_platform()
        platform.async_register_entity_service(
            SERVICE_ATTEMPT_BIND,
            {},
            "async_attempt_bind",
        )
        return

    connection = entry_data["connection"]
    zones = entry_data["zones"]
    entities_registry = entry_data["entities"]
    device_id = config.unique_id or f"au7000_{connection.host}"
    device_info = _au7000_device_info(device_id)

    entities = []
    zone_ids = []

    for zone in zones:
        name = zone.get("name")
        zone_id = zone.get("zone_id")
        sources = zone.get("sources")

        if not name or not zone_id:
            _LOGGER.error("Invalid zone configuration: %s", zone)
            continue

        zone_ids.append(f"{zone_id}")
        entity = LegrandDigitalAudio(
            name,
            connection,
            zone_id,
            sources,
            entities_registry,
            config.entry_id,
            device_info,
        )
        entities_registry[zone_id] = entity
        entities.append(entity)

    # Aggregate "all" entity. Use the first zone's sources as the canonical list
    # (config_flow assigns the same SourceList to every zone today).
    aggregate_sources = zones[0]["sources"] if zones else []
    aggregate = LegrandDigitalAudio(
        ALL_KEY,
        connection,
        zone_ids,
        aggregate_sources,
        entities_registry,
        config.entry_id,
        device_info,
    )
    entities_registry[ALL_KEY] = aggregate
    entities.append(aggregate)

    async_add_entities(entities)


class LegrandDigitalAudio(MediaPlayerEntity):
    """Representation of a media player controlled via a socket."""

    def __init__(
        self,
        name,
        connection,
        zone_id,
        sources,
        entities_registry,
        entry_id,
        device_info,
    ):
        """Initialize the media player."""
        self._name = f"{name}"
        self._connection = connection
        self._zone_id = zone_id
        self._state = MediaPlayerState.OFF
        self._volume = 0.05
        self._source = sources[0]["Name"] if sources else None
        self._source_list = sources or []
        self._is_muted = False
        self._entities_registry = entities_registry
        self._is_aggregate = isinstance(zone_id, list)
        self._attr_device_info = device_info

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
            self._attr_suggested_object_id = f"legrand_{name}"

    def _get_next_command_id(self):
        """Generate the next unique command ID."""
        self._command_id += 1
        return self._command_id

    async def _send_command(self, command):
        """Send a command to the device via the shared connection.

        All socket I/O, framing, reconnection and the greeting handshake are
        handled by the connection manager; this is a thin passthrough.
        """
        return await self._connection.send(command)

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
            _LOGGER.error("Failed to parse response: %s", e)

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
                _LOGGER.warning("Aggregate update: peer refresh failed: %s", e)

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
    def available(self):
        """Return True when the shared connection to the device is up."""
        return self._connection.available

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


async def _async_dim_connecting(hass, udn: str) -> bool | None:
    """Return AU7000 DIM Connecting for this UDN, if an AU7000 entry exists."""
    domain_data = hass.data.get(DOMAIN) or {}
    for entry_data in domain_data.values():
        if not isinstance(entry_data, dict):
            continue
        if entry_data.get(CONF_DEVICE_TYPE) != DEVICE_TYPE_AU7000:
            continue
        connection = entry_data.get("connection")
        if connection is None:
            continue
        # Use a high ID so we do not collide with zone media_player command IDs.
        response = await connection.send(
            json.dumps({"ID": 91001, "Service": "ListSources"})
        )
        if not isinstance(response, dict):
            return None
        sources = response.get("SourceList") or []
        want = udn.lower().replace("-", "")
        if want.startswith("uuid:"):
            want = want[5:]
        for source in sources:
            if source.get("Type") != "DIM1":
                continue
            upnp_id = str(source.get("UPnP ID") or "").lower().replace("-", "")
            if want and want not in upnp_id and upnp_id not in want:
                continue
            return bool(source.get("Connecting"))
        for source in sources:
            if source.get("Type") == "DIM1":
                return bool(source.get("Connecting"))
    return None


class LegrandNuvoZone(MediaPlayerEntity):
    """An AU7001 streaming zone controlled over UPnP.

    Modeled as its own device/media_player (separate from the AU7000 zones):
    a target you can stream services like Pandora or Spotify to.
    """

    _STATE_MAP = {
        "playing": MediaPlayerState.PLAYING,
        "paused": MediaPlayerState.PAUSED,
        "idle": MediaPlayerState.IDLE,
    }

    def __init__(self, zone, entry_id):
        """Initialize the AU7001 streaming zone entity."""
        self._zone = zone
        self._entry_id = entry_id
        self._dim_connecting: bool | None = None
        self._attr_name = DEFAULT_DEVICE_NAME_AU7001
        self._attr_unique_id = f"{DOMAIN}_{zone.udn}"
        self._attr_suggested_object_id = "legrand_digital_audio_module"
        connections = {(CONNECTION_UPNP, zone.udn)} if zone.udn else set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, zone.udn)},
            name=DEFAULT_DEVICE_NAME_AU7001,
            manufacturer="Legrand / NuVo",
            model="AU7001",
            configuration_url=zone.configuration_url,
            connections=connections,
        )

    async def async_update(self):
        """Poll the device for its latest state."""
        if getattr(self._zone, "_closed", False):
            return
        await self._zone.async_update()
        if getattr(self._zone, "_closed", False):
            return
        self._dim_connecting = await _async_dim_connecting(self.hass, self._zone.udn)

    async def async_attempt_bind(self) -> None:
        """Entity service: run SystemCreate, then ask the user to press the button."""
        result = await self._zone.async_attempt_bind()
        _LOGGER.info(
            "attempt_bind on %s → %s (SystemID=%s): %s",
            self.name,
            result.get("status"),
            result.get("system_id"),
            result.get("message"),
        )
        self.async_write_ha_state()

    @property
    def available(self):
        """Return True when the device is reachable."""
        return self._zone.available

    @property
    def extra_state_attributes(self):
        """Expose bind health for diagnostics and automations."""
        attrs = {
            "bind_status": self._zone.bind_status,
            "bind_hint": self._zone.bind_hint,
            "active": self._zone.active,
            "connecting": self._zone.connecting,
            "system_id": self._zone.system_id,
            "member_id": self._zone.member_id,
            "zone_title": self._zone.zone_title,
            "host": self._zone.host,
        }
        if self._dim_connecting is not None:
            attrs["dim_connecting"] = self._dim_connecting
        return attrs

    @property
    def state(self):
        """Return the playback state."""
        return self._STATE_MAP.get(self._zone.state, MediaPlayerState.IDLE)

    @property
    def volume_level(self):
        return self._zone.volume_level

    @property
    def is_volume_muted(self):
        return self._zone.is_muted

    @property
    def media_title(self):
        return self._zone.media_title

    @property
    def media_artist(self):
        return self._zone.media_artist

    @property
    def media_album_name(self):
        return self._zone.media_album

    @property
    def media_content_type(self):
        """Return content type so Lovelace shows artist under the title.

        Home Assistant's media cards only render media_artist when
        media_content_type is "music" (see computeMediaDescription). Pandora
        and other on-device sources never go through play_media, so without
        this the card shows title + art but hides the artist.
        """
        if self._zone.media_title or self._zone.media_artist or self._zone.media_album:
            return MediaType.MUSIC
        return getattr(self, "_attr_media_content_type", None)

    @property
    def media_image_url(self):
        return self._zone.media_image_url

    @property
    def media_duration(self):
        return self._zone.media_duration

    @property
    def media_position(self):
        return self._zone.media_position

    @property
    def supported_features(self):
        features = (
            MediaPlayerEntityFeature.PLAY
            | MediaPlayerEntityFeature.PAUSE
            | MediaPlayerEntityFeature.STOP
            | MediaPlayerEntityFeature.NEXT_TRACK
            | MediaPlayerEntityFeature.PREVIOUS_TRACK
            | MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.BROWSE_MEDIA
        )
        if self._zone.is_active:
            features |= MediaPlayerEntityFeature.PLAY_MEDIA
        return features

    async def async_browse_media(
        self,
        media_content_type: MediaType | str | None = None,
        media_content_id: str | None = None,
    ) -> BrowseMedia:
        """Browse Pandora and other services configured on the AU7001."""
        if not self._zone.is_active:
            raise BrowseError(
                "AU7001 is inactive. Complete bind in the Digital Audio app "
                "(bind button + solid white LED) before browsing."
            )

        object_id = media_content_id or NUVO_BROWSE_ROOT
        result = await self._zone.async_browse(object_id)
        if result is None:
            raise BrowseError(
                self._zone.last_browse_error or f"Browse failed for {object_id}"
            )

        children: list[BrowseMedia] = []
        for item in result.items:
            if item.is_playable:
                children.append(
                    BrowseMedia(
                        title=item.title,
                        media_class=MediaClass.MUSIC,
                        media_content_id=item.object_id,
                        media_content_type=MediaType.MUSIC,
                        can_play=True,
                        can_expand=False,
                    )
                )
            elif item.is_container:
                children.append(
                    BrowseMedia(
                        title=item.title,
                        media_class=MediaClass.DIRECTORY,
                        media_content_id=item.object_id,
                        media_content_type=MediaType.URL,
                        can_play=False,
                        can_expand=True,
                    )
                )

        return BrowseMedia(
            title=result.title,
            media_class=MediaClass.DIRECTORY,
            media_content_id=result.object_id,
            media_content_type=MediaType.URL,
            children=children,
            can_play=False,
            can_expand=True,
        )

    async def async_media_play(self):
        await self._zone.async_play()
        self.async_write_ha_state()

    async def async_media_pause(self):
        await self._zone.async_pause()
        self.async_write_ha_state()

    async def async_media_stop(self):
        await self._zone.async_stop()
        self.async_write_ha_state()

    async def async_media_next_track(self):
        await self._zone.async_next()

    async def async_media_previous_track(self):
        await self._zone.async_previous()

    async def async_set_volume_level(self, volume):
        await self._zone.async_set_volume(volume)
        self.async_write_ha_state()

    async def async_mute_volume(self, mute):
        await self._zone.async_set_mute(mute)
        self.async_write_ha_state()

    async def async_play_media(self, media_type, media_id, **kwargs):
        """Play an HTTP stream URL or a browsed NuVo container item."""
        if not self._zone.is_active:
            _LOGGER.warning(
                "%s is inactive; bind the AU7001 before playback", self.name
            )
            return

        media_id = (media_id or "").strip()
        if media_id.startswith(("http://", "https://")):
            extra = kwargs.get("extra") or {}
            metadata = extra.get("metadata") or {}
            title = _meta_text(
                metadata.get("title")
                or extra.get("title")
                or kwargs.get("media_title")
            ) or "Stream"
            artist = _meta_text(metadata.get("artist"))
            album = _meta_text(
                metadata.get("album") or metadata.get("albumName")
            )
            image_url = _stream_image_url(metadata, extra)
            _LOGGER.info(
                "Playing stream on %s from %s (title=%s artist=%s)",
                self.name,
                media_id.split("?")[0][:120],
                title,
                artist or "-",
            )
            if await self._zone.async_play_uri(
                media_id,
                title=title,
                artist=artist,
                album=album,
                image_url=image_url,
            ):
                self._attr_media_content_id = media_id
                self._attr_media_content_type = media_type or "music"
                # Keep HA entity metadata even if the AU7001 reports NuVo DIDL.
                if title:
                    self._zone.media_title = title
                if artist:
                    self._zone.media_artist = artist
                if album:
                    self._zone.media_album = album
                if image_url:
                    self._zone.media_image_url = image_url
                self.async_write_ha_state()
            else:
                _LOGGER.error("Failed to stream URL on %s", self.name)
            return

        if await self._zone.async_play_browse_item(media_id):
            self.async_write_ha_state()
        else:
            _LOGGER.error("Failed to start playback for %s", media_id)
