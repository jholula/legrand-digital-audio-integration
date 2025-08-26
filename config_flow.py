from homeassistant import config_entries
import voluptuous as vol
from .const import DOMAIN  # Replace with your integration's domain
import logging
import socket
import json

_LOGGER = logging.getLogger(__name__)

# Define the schema for the initial configuration form
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required("host"): str,  # Host/IP address of the device
        vol.Required("port", default=2112): int,  # Port number
    }
)

class LegrandDigitalAudioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for your custom integration."""

    VERSION = 1  # Increment this if you make breaking changes to the config flow
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            try:
                # Fetch all devices (zones) from the API
                devices = await self._fetch_devices(user_input["host"], user_input["port"])
                _LOGGER.debug("Fetched devices: %s", devices)

                # Automatically create a configuration entry with all devices
                return self.async_create_entry(
                    title="Legrand Digital Audio",
                    data={
                        "host": user_input["host"],
                        "port": user_input["port"],
                        "zones": devices  # Pass all devices to the config entry
                    },
                )
            except Exception as e:
                _LOGGER.error("Error fetching devices: %s", e)
                errors["base"] = "cannot_connect"

        return self.async_show_form(
            step_id="user", data_schema=CONFIG_SCHEMA, errors=errors
        )

    async def _fetch_devices(self, host, port):
        """Fetch all zones (devices) from the Legrand Digital Audio system."""
        _LOGGER.debug("Fetching zones from %s:%s", host, port)

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect((host, port))
                _LOGGER.debug("Connected to %s:%s", host, port)

                # Receive the initial greeting
                greeting = s.recv(1024)  # Adjust buffer size as needed
                _LOGGER.debug(f"Received: {greeting.decode('utf-8').replace('\x00','').strip()}")

                # Command to list all zones
                command = json.dumps({
                    "ID": 3,
                    "Service": "ListSources"
                }) + "\n"

                s.sendall(command.encode('utf-8'))
                _LOGGER.debug(f"Sent: {command}")

                # Read the response
                response_data = s.recv(1024)  # Adjust buffer size as needed
                sources = json.loads(response_data.decode("utf-8").replace('\x00', '').strip())
                _LOGGER.debug("Received response: %s", sources)

                # Command to list all zones
                command = json.dumps({
                    "ID": 4,
                    "Service": "ListZones"
                }) + "\n"

                s.sendall(command.encode('utf-8'))
                _LOGGER.debug(f"Sent: {command}")

                # Read the response
                response_data = s.recv(1024)  # Adjust buffer size as needed
                response = response_data.decode("utf-8").replace('\x00', '').strip()
                _LOGGER.debug("Received response: %s", response)


            # Parse the response
            devices = []

            try:
                response_json = json.loads(response)

                for i in response_json['ZoneList']:
                    zone_id = i.get("ZID")
                    zone_name = i.get("Name", f"Zone {zone_id}")
                    _LOGGER.debug(f'Configuring zone: {zone_name}')
                    if zone_id:
                        devices.append({"zone_id": zone_id, "name": f'{zone_name.replace(' ','_')}', "sources": sources['SourceList']})

            except json.JSONDecodeError:
                _LOGGER.error("Failed to parse JSON: %s", response)

            if not devices:
                raise Exception("No zones found")
            return devices

        except Exception as e:
            _LOGGER.error("Error fetching zones: %s", e)
            raise