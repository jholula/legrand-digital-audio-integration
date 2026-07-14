from __future__ import annotations

from homeassistant import config_entries
from homeassistant.helpers.device_registry import format_mac
import voluptuous as vol
from .const import DOMAIN, DEFAULT_PORT
import logging
import socket
import json

_LOGGER = logging.getLogger(__name__)

# Sentinel option shown in the discovery dropdown to fall back to manual entry.
MANUAL_ENTRY = "__manual__"

# Bounds for the active subnet scan.
SCAN_TIMEOUT = 0.6  # per-host TCP connect timeout (seconds)
SCAN_WORKERS = 64   # concurrent probe threads

# Timeout when validating a chosen/entered host against the JSON API.
FETCH_TIMEOUT = 10


class LegrandDigitalAudioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Legrand Digital Audio integration."""

    VERSION = 1  # Increment this if you make breaking changes to the config flow
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self):
        self._discovered_hosts: list[str] = []
        self._host: str | None = None
        self._zones: list | None = None

    # ------------------------------------------------------------------
    # User-initiated flow: actively scan the LAN, then let the user pick.
    # ------------------------------------------------------------------
    async def async_step_user(self, user_input=None):
        """Handle the initial step: scan for devices and present a picker."""
        if user_input is None:
            # Actively probe the local subnet for anything answering the
            # Legrand JSON API on the fixed control port.
            self._discovered_hosts = await self.hass.async_add_executor_job(
                self._scan_for_devices
            )
            # Drop any host that's already configured.
            configured = {
                entry.data.get("host")
                for entry in self._async_current_entries()
            }
            self._discovered_hosts = [
                h for h in self._discovered_hosts if h not in configured
            ]

            if not self._discovered_hosts:
                # Nothing found automatically -> go straight to manual entry.
                return await self.async_step_manual()

            options = {host: host for host in self._discovered_hosts}
            options[MANUAL_ENTRY] = "Enter IP address manually"
            schema = vol.Schema(
                {
                    vol.Required(
                        "host", default=self._discovered_hosts[0]
                    ): vol.In(options)
                }
            )
            return self.async_show_form(step_id="user", data_schema=schema)

        host = user_input["host"]
        if host == MANUAL_ENTRY:
            return await self.async_step_manual()

        errors = {}
        try:
            self._zones = await self.hass.async_add_executor_job(
                self._fetch_zones, host, DEFAULT_PORT
            )
        except Exception as e:  # noqa: BLE001 - surfaced to the user as an error
            _LOGGER.error("Error fetching devices from %s: %s", host, e)
            errors["base"] = "cannot_connect"
        else:
            return await self._create_entry(host)

        # Re-show the picker with the error.
        options = {h: h for h in self._discovered_hosts}
        options[MANUAL_ENTRY] = "Enter IP address manually"
        schema = vol.Schema(
            {vol.Required("host", default=host): vol.In(options)}
        )
        return self.async_show_form(
            step_id="user", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    # Manual entry fallback (host only; port is fixed at 2112).
    # ------------------------------------------------------------------
    async def async_step_manual(self, user_input=None):
        """Handle manual IP entry."""
        errors = {}
        if user_input is not None:
            host = user_input["host"]
            try:
                self._zones = await self.hass.async_add_executor_job(
                    self._fetch_zones, host, DEFAULT_PORT
                )
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Error fetching devices from %s: %s", host, e)
                errors["base"] = "cannot_connect"
            else:
                return await self._create_entry(host)

        schema = vol.Schema({vol.Required("host"): str})
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    # ------------------------------------------------------------------
    # DHCP discovery: HA hands us the device when it sees the Legrand OUI.
    # ------------------------------------------------------------------
    async def async_step_dhcp(self, discovery_info):
        """Handle discovery via DHCP (matched on the Legrand MAC OUI)."""
        host = discovery_info.ip
        mac = discovery_info.macaddress

        await self.async_set_unique_id(format_mac(mac))
        self._abort_if_unique_id_configured(updates={"host": host})

        self._host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input=None):
        """Confirm adding a discovered device."""
        errors = {}
        if user_input is not None:
            try:
                self._zones = await self.hass.async_add_executor_job(
                    self._fetch_zones, self._host, DEFAULT_PORT
                )
            except Exception as e:  # noqa: BLE001
                _LOGGER.error(
                    "Error fetching devices from %s: %s", self._host, e
                )
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title="Legrand Digital Audio",
                    data={
                        "host": self._host,
                        "port": DEFAULT_PORT,
                        "zones": self._zones,
                    },
                )

        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"host": self._host},
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    async def _create_entry(self, host):
        """Set a stable unique id (from MAC when resolvable) and create entry."""
        mac = await self.hass.async_add_executor_job(self._get_mac, host)
        if mac:
            await self.async_set_unique_id(format_mac(mac))
            self._abort_if_unique_id_configured(updates={"host": host})
        else:
            await self.async_set_unique_id(f"{DOMAIN}_{host}")
            self._abort_if_unique_id_configured(updates={"host": host})

        return self.async_create_entry(
            title="Legrand Digital Audio",
            data={
                "host": host,
                "port": DEFAULT_PORT,
                "zones": self._zones,
            },
        )

    @staticmethod
    def _get_mac(host):
        """Best-effort MAC lookup for a host (uses getmac if available)."""
        try:
            from getmac import get_mac_address

            return get_mac_address(ip=host)
        except Exception:  # noqa: BLE001 - MAC is optional, degrade gracefully
            return None

    @staticmethod
    def _local_ipv4():
        """Determine this host's primary IPv4 address (no traffic is sent)."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            # Does not actually send packets; just selects the outbound iface.
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        except Exception:  # noqa: BLE001
            return None
        finally:
            s.close()

    def _scan_for_devices(self):
        """Scan the local /24 for hosts answering the Legrand greeting on 2112."""
        from concurrent.futures import ThreadPoolExecutor

        local_ip = self._local_ipv4()
        if not local_ip:
            _LOGGER.warning("Could not determine local IP; skipping scan")
            return []

        prefix = local_ip.rsplit(".", 1)[0]
        candidates = [f"{prefix}.{i}" for i in range(1, 255)]
        found: list[str] = []

        def probe(ip):
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(SCAN_TIMEOUT)
                    s.connect((ip, DEFAULT_PORT))
                    s.settimeout(1.0)
                    banner = s.recv(256)
                # The device announces itself with a JSON greeting on connect.
                if b"Greeting" in banner or b"Nuvo" in banner:
                    return ip
            except Exception:  # noqa: BLE001 - most hosts simply won't answer
                return None
            return None

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
            for result in executor.map(probe, candidates):
                if result:
                    found.append(result)

        _LOGGER.debug("Scan found Legrand devices: %s", found)
        return found

    def _fetch_zones(self, host, port):
        """Fetch all zones (devices) from the Legrand Digital Audio system.

        This is a blocking call intended to be run in an executor.
        """
        _LOGGER.debug("Fetching zones from %s:%s", host, port)

        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(FETCH_TIMEOUT)
                s.connect((host, port))
                _LOGGER.debug("Connected to %s:%s", host, port)

                # Receive the initial greeting
                greeting = s.recv(1024)  # Adjust buffer size as needed
                greeting_text = greeting.decode("utf-8").replace("\x00", "").strip()
                _LOGGER.debug("Received: %s", greeting_text)

                # Command to list all sources
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
                    _LOGGER.debug("Configuring zone: %s", zone_name)
                    if zone_id:
                        normalized_name = zone_name.replace(" ", "_")
                        devices.append({
                            "zone_id": zone_id,
                            "name": normalized_name,
                            "sources": sources["SourceList"],
                        })

            except json.JSONDecodeError:
                _LOGGER.error("Failed to parse JSON: %s", response)

            if not devices:
                raise Exception("No zones found")
            return devices

        except Exception as e:
            _LOGGER.error("Error fetching zones: %s", e)
            raise
