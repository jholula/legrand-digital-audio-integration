import socket
import json
import logging
import asyncio
from homeassistant.components.media_player import (
    MediaPlayerDeviceClass,
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaPlayerState,
)

from homeassistant.const import STATE_IDLE, STATE_PLAYING, STATE_OFF



from datetime import timedelta

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SCAN_INTERVAL = timedelta(seconds=30)  # Set the update interval to 30 seconds
SOCKET_TIMEOUT = 10  # Timeout for socket operations in seconds

async def async_setup_platform(hass, config, async_add_entities, discovery_info=None):
    """Set up the Legrand Digital Audio platform."""
    _LOGGER.debug(hass.data[DOMAIN])
    shared_socket = hass.data[DOMAIN]["socket"]
    zones = hass.data[DOMAIN]["zones"]

    entities = []
    for zone in zones:
        name = zone.get("name")
        zone_id = zone.get("zone_id")
        default_source = zone.get("default_source")

        if not name or not zone_id:
            _LOGGER.error(f"Invalid zone configuration: {zone}")
            continue
        entities.append(LegrandDigitalAudio(name, shared_socket, zone_id, default_source))

    async_add_entities(entities)

class LegrandDigitalAudio(MediaPlayerEntity):
    """Representation of a media player controlled via a socket."""

    def __init__(self, name, shared_socket, zone_id, default_source):
        """Initialize the media player."""
        self._name = f'legrand_audio_{name}'
        self._socket = shared_socket
        self._zone_id = zone_id
        self._state = STATE_OFF
        self._volume = .05
        self._source = default_source
        self._default_source = default_source
        self._is_muted = False
        self._lock = asyncio.Lock()  # Add a lock for socket synchronization
        self._command_id = int(f"{zone_id}00000000")

    @property
    def unique_id(self):
        """Return a unique ID for this entity."""
        return f"{DOMAIN}_{self._zone_id}"

    def _get_next_command_id(self):
        """Generate the next unique command ID."""
        self._command_id += 1
        return self._command_id

    async def _send_command(self, command):
        """Send a command to the device and wait for the response."""
        async with self._lock:  # Ensure only one coroutine accesses the socket at a time
            try:
                # Parse the command to extract the command ID
                command_data = json.loads(command)
                sent_command_id = command_data.get("ID")

                # Send the command
                _LOGGER.debug(f"Sent: {command}")
                await asyncio.get_event_loop().sock_sendall(self._socket, str(command + "\n").encode('utf-8'))

                # Wait for the response
                buffer = ""  # Buffer to store incomplete data
                while True:
                # Check if the timeout has been exceeded
                    # if time.time() - start_time > timeout:
                    #     _LOGGER.error(f"Timeout waiting for response to command ID {sent_command_id}")
                    #     return None
                    # Receive data from the socket
                    data = await asyncio.get_event_loop().sock_recv(self._socket, 1024)
                    if not data:
                        _LOGGER.error("Socket connection closed by the device.")
                        return None

                    buffer += data.decode("utf-8")  # Append received data to the buffer

                    # Split the buffer into individual messages based on the delimiter
                    messages = buffer.split("\x00")
                    buffer = messages.pop()  # Keep the last (incomplete) part in the buffer

                    for message in messages:
                        # Parse the JSON object
                        try:
                            response_json = json.loads(message)
                            _LOGGER.debug(f"Received: {response_json}")

                            # Check if the response ID matches the sent command ID
                            response_id = response_json.get("ID")
                            if response_id == sent_command_id:
                                return response_json  # Return the matching response
                            else:
                                _LOGGER.warning(f"Response ID {response_id} does not match sent ID {sent_command_id}. Ignoring.")
                        except json.JSONDecodeError:
                            _LOGGER.error(f"Failed to parse JSON: {message}")
            except Exception as e:
                _LOGGER.error(f"Socket communication error: {e}")
                return None

    def _parse_response(self, response):
        """Parse the response from the device and update the state."""
        try:
            if "PropertyList" in response:
                properties = response["PropertyList"]
                self._state = STATE_PLAYING if properties.get("Power") else STATE_OFF
                self._volume = round(properties.get("Volume", self._volume)/100, 2)
                self._source = properties.get("Source", self._source)
                self._is_muted = properties.get("Muted", self._is_muted)
        except Exception as e:
            _LOGGER.error(f"Failed to parse response: {e}")

    async def async_update(self):
        """Fetch the latest state from the device."""
        command_id = self._get_next_command_id()
        command = json.dumps({"ID": command_id, "Service": "ReportZoneProperties", "ZID": f"Z{self._zone_id}"})
        response = await self._send_command(command)
        if response:
            self._parse_response(response)

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
        """Return the Source of the speakers."""
        return self._source

    @property
    def is_volume_muted(self):
        """Return True if the volume is muted."""
        return self._is_muted

    @property
    def supported_features(self):
        """Return the supported features."""
        return (
            # MediaPlayerEntityFeature.PLAY
            # | MediaPlayerEntityFeature.PAUSE
            # | MediaPlayerEntityFeature.STOP
            MediaPlayerEntityFeature.VOLUME_SET
            | MediaPlayerEntityFeature.VOLUME_MUTE
            | MediaPlayerEntityFeature.TURN_ON
            | MediaPlayerEntityFeature.TURN_OFF
        )


    async def async_turn_on(self):
        """Turn the media player on."""
        command_id = self._get_next_command_id()
        command = json.dumps({"ID": command_id, "Service": "SetZoneProperty", "ZID": f"Z{self._zone_id}", "PropertyList": {"Power": True, "Source": f'S{self._default_source}'}})
        await self._send_command(command)
        self._state = STATE_IDLE

    async def async_turn_off(self):
        """Turn the media player on."""
        command_id = self._get_next_command_id()
        command = json.dumps({"ID": command_id, "Service": "SetZoneProperty", "ZID": f"Z{self._zone_id}", "PropertyList": {"Power": False}})
        await self._send_command(command)
        self._state = STATE_IDLE

    async def async_set_volume_level(self, volume):
        """Set the volume level."""
        command_id = self._get_next_command_id()
        command = json.dumps({"ID": command_id, "Service": "SetZoneProperty", "ZID": f"Z{self._zone_id}", "PropertyList": {"Volume": int(volume*100)}})
        await self._send_command(command)
        self._volume = volume
        self.async_write_ha_state()

    async def async_mute_volume(self, mute):
        """Mute or unmute the volume."""
        command_id = self._get_next_command_id()
        command = json.dumps({"ID": command_id, "Service": "SetZoneProperty", "ZID": f"Z{self._zone_id}", "PropertyList": {"Mute": mute}})
        await self._send_command(command)
        self._is_muted = mute
        self.async_write_ha_state()

    # async def async_set_source(self, source):
    #     """Set Zone Source."""
    #     command_id = self._get_next_command_id()
    #     command = json.dumps({"ID": command_id, "Service": "SetZoneProperty", "ZID": f"Z{self._zone_id}", "PropertyList": {"Source": f'S{source}'}})
    #     await self._send_command(command)
    #     self._is_muted = mute
    #     self.async_write_ha_state()