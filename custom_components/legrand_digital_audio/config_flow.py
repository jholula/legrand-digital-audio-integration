from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
from urllib.parse import urlsplit
from urllib.request import urlopen
from xml.etree import ElementTree

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers.device_registry import format_mac

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

_LOGGER = logging.getLogger(__name__)

CHOICE_MANUAL_AU7000 = "manual_au7000"
CHOICE_MANUAL_AU7001 = "manual_au7001"
CHOICE_PREFIX_AU7000 = "au7000|"
CHOICE_PREFIX_AU7001 = "au7001|"

SCAN_TIMEOUT = 0.6
SCAN_WORKERS = 64
FETCH_TIMEOUT = 10
SSDP_TIMEOUT = 4


class LegrandDigitalAudioConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for the Legrand Digital Audio integration."""

    VERSION = 1
    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL

    def __init__(self) -> None:
        self._discovered_au7000: list[str] = []
        self._discovered_au7001: list[dict] = []
        self._host: str | None = None
        self._zones: list | None = None
        self._ssdp_location: str | None = None
        self._ssdp_udn: str | None = None
        self._ssdp_name: str | None = None
        self._companion_offer: dict | None = None
        self._add_companion = False

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    async def async_step_user(self, user_input=None):
        """Scan for AU7000 and AU7001 devices and let the user pick one."""
        prefill = self.context.get("prefill_companion")
        if prefill and user_input is None:
            return await self._async_handle_prefill_companion(prefill)

        if user_input is None:
            await self._async_discover_devices()
            return self._async_show_device_picker()

        choice = user_input["device"]
        if choice == CHOICE_MANUAL_AU7000:
            return await self.async_step_manual()
        if choice == CHOICE_MANUAL_AU7001:
            return await self.async_step_manual_au7001()

        if choice.startswith(CHOICE_PREFIX_AU7000):
            host = choice[len(CHOICE_PREFIX_AU7000) :]
            return await self._async_setup_au7000(host)

        if choice.startswith(CHOICE_PREFIX_AU7001):
            udn = choice[len(CHOICE_PREFIX_AU7001) :]
            device = next(
                (d for d in self._discovered_au7001 if d["udn"] == udn), None
            )
            if device is None:
                return self._async_show_device_picker(errors={"base": "cannot_connect"})
            return await self._async_begin_au7001(device)

        return self._async_show_device_picker(errors={"base": "cannot_connect"})

    async def async_step_manual(self, user_input=None):
        """Manual entry for the AU7000 distribution module."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input["host"]
            port = user_input["port"]
            try:
                self._zones = await self.hass.async_add_executor_job(
                    self._fetch_zones, host, port
                )
            except Exception as e:  # noqa: BLE001
                _LOGGER.error("Error fetching devices from %s: %s", host, e)
                errors["base"] = "cannot_connect"
            else:
                return await self._async_maybe_offer_companion_au7000(
                    host, port
                )

        schema = vol.Schema(
            {
                vol.Required("host"): str,
                vol.Required("port", default=DEFAULT_PORT): int,
            }
        )
        return self.async_show_form(
            step_id="manual", data_schema=schema, errors=errors
        )

    async def async_step_manual_au7001(self, user_input=None):
        """Manual entry for the AU7001 by IP address (SSDP lookup)."""
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input["host"].strip()
            device = await self.hass.async_add_executor_job(
                self._discover_au7001_at_host, host
            )
            if device is None:
                errors["base"] = "cannot_connect_au7001"
            else:
                return await self._async_begin_au7001(device)

        schema = vol.Schema({vol.Required("host"): str})
        return self.async_show_form(
            step_id="manual_au7001", data_schema=schema, errors=errors
        )

    async def async_step_recommend_companion(self, user_input=None):
        """Offer to add the other module discovered on the network."""
        if self._companion_offer is None:
            return self.async_abort(reason="cannot_connect")

        if user_input is not None:
            self._add_companion = user_input.get("add_companion", False)
            primary = self._companion_offer["primary"]
            if primary["type"] == DEVICE_TYPE_AU7000:
                return await self._async_create_au7000_entry(
                    primary["host"], primary.get("port", DEFAULT_PORT)
                )
            return await self._async_create_au7001_entry()

        schema = vol.Schema({vol.Optional("add_companion", default=True): bool})
        return self.async_show_form(
            step_id="recommend_companion",
            data_schema=schema,
            description_placeholders={
                "companion_label": self._companion_offer["label"],
            },
        )

    async def async_step_dhcp(self, discovery_info):
        """Handle discovery via DHCP (AU7000 distribution module)."""
        host = discovery_info.ip
        mac = discovery_info.macaddress

        await self.async_set_unique_id(format_mac(mac))
        self._abort_if_unique_id_configured(updates={"host": host})

        self._host = host
        self.context["title_placeholders"] = {"host": host}
        return await self.async_step_discovery_confirm()

    async def async_step_discovery_confirm(self, user_input=None):
        """Confirm adding a discovered AU7000."""
        errors: dict[str, str] = {}
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
                await self._async_discover_devices()
                return await self._async_maybe_offer_companion_au7000(
                    self._host, DEFAULT_PORT
                )

        return self.async_show_form(
            step_id="discovery_confirm",
            description_placeholders={"host": self._host},
            errors=errors,
        )

    async def async_step_ssdp(self, discovery_info):
        """Handle SSDP discovery of an AU7001 streaming module."""
        location = getattr(discovery_info, "ssdp_location", None)
        upnp = getattr(discovery_info, "upnp", {}) or {}
        udn = upnp.get("UDN")
        friendly = upnp.get("friendlyName") or "Legrand AU7001"

        if not location or not udn:
            return self.async_abort(reason="cannot_connect")

        await self.async_set_unique_id(udn)
        self._abort_if_unique_id_configured(updates={"location": location})

        self._ssdp_location = location
        self._ssdp_udn = udn
        self._ssdp_name = friendly
        self.context["title_placeholders"] = {
            "host": self._host_from_location(location),
        }
        await self._async_discover_devices()
        return await self.async_step_ssdp_confirm()

    async def async_step_ssdp_confirm(self, user_input=None):
        """Confirm adding an AU7001 streaming module."""
        if user_input is not None:
            companion_host = (
                self._discovered_au7000[0] if self._discovered_au7000 else None
            )
            if companion_host:
                self._companion_offer = {
                    "primary": {
                        "type": DEVICE_TYPE_AU7001,
                        "location": self._ssdp_location,
                        "udn": self._ssdp_udn,
                        "name": self._ssdp_name,
                    },
                    "companion": {
                        "type": DEVICE_TYPE_AU7000,
                        "host": companion_host,
                        "port": DEFAULT_PORT,
                    },
                    "label": self._label_au7000(companion_host),
                }
                return await self.async_step_recommend_companion()
            return await self._async_create_au7001_entry()

        return self.async_show_form(
            step_id="ssdp_confirm",
            description_placeholders={
                "name": self._ssdp_name,
                "device_name": DEFAULT_DEVICE_NAME_AU7001,
            },
        )

    # ------------------------------------------------------------------
    # Internal setup helpers
    # ------------------------------------------------------------------
    async def _async_discover_devices(self) -> None:
        """Refresh cached AU7000/AU7001 discovery lists."""
        au7000, au7001 = await self.hass.async_add_executor_job(
            self._scan_for_all_devices
        )
        configured_hosts = self._configured_au7000_hosts()
        configured_udns = self._configured_au7001_udns()
        self._discovered_au7000 = [h for h in au7000 if h not in configured_hosts]
        self._discovered_au7001 = [
            d for d in au7001 if d["udn"] not in configured_udns
        ]

    def _async_show_device_picker(self, errors=None):
        """Show the unified device picker."""
        options: dict[str, str] = {}
        for host in self._discovered_au7000:
            key = f"{CHOICE_PREFIX_AU7000}{host}"
            options[key] = self._label_au7000(host)
        for device in self._discovered_au7001:
            key = f"{CHOICE_PREFIX_AU7001}{device['udn']}"
            options[key] = self._label_au7001(device)

        options[CHOICE_MANUAL_AU7000] = self._label_manual_au7000()
        options[CHOICE_MANUAL_AU7001] = self._label_manual_au7001()

        default = next(iter(options))
        schema = vol.Schema({vol.Required("device", default=default): vol.In(options)})
        return self.async_show_form(
            step_id="user",
            data_schema=schema,
            errors=errors or {},
            description_placeholders={
                "au7000_count": str(len(self._discovered_au7000)),
                "au7001_count": str(len(self._discovered_au7001)),
            },
        )

    async def _async_setup_au7000(self, host: str, port: int = DEFAULT_PORT):
        """Validate an AU7000 and continue setup."""
        try:
            self._zones = await self.hass.async_add_executor_job(
                self._fetch_zones, host, port
            )
        except Exception as e:  # noqa: BLE001
            _LOGGER.error("Error fetching devices from %s: %s", host, e)
            await self._async_discover_devices()
            return self._async_show_device_picker(errors={"base": "cannot_connect"})

        return await self._async_maybe_offer_companion_au7000(host, port)

    async def _async_maybe_offer_companion_au7000(
        self, host: str, port: int = DEFAULT_PORT
    ):
        """After AU7000 validation, offer an AU7001 companion if present."""
        primary = {"type": DEVICE_TYPE_AU7000, "host": host, "port": port}
        companion = self._discovered_au7001[0] if self._discovered_au7001 else None
        if companion:
            self._companion_offer = {
                "primary": primary,
                "companion": {
                    "type": DEVICE_TYPE_AU7001,
                    "location": companion["location"],
                    "udn": companion["udn"],
                    "name": companion["name"],
                },
                "label": self._label_au7001(companion),
            }
            return await self.async_step_recommend_companion()
        return await self._async_create_au7000_entry(host, port)

    async def _async_begin_au7001(self, device: dict):
        """Prepare AU7001 context and show the confirmation step."""
        self._ssdp_location = device["location"]
        self._ssdp_udn = device["udn"]
        self._ssdp_name = device["name"]
        return await self.async_step_ssdp_confirm()

    async def _async_create_au7000_entry(self, host: str, port: int = DEFAULT_PORT):
        """Create an AU7000 config entry, optionally queueing a companion flow."""
        mac = await self.hass.async_add_executor_job(self._get_mac, host)
        if mac:
            await self.async_set_unique_id(format_mac(mac))
            self._abort_if_unique_id_configured(updates={"host": host})
        else:
            await self.async_set_unique_id(f"{DOMAIN}_au7000_{host}")
            self._abort_if_unique_id_configured(updates={"host": host})

        if self._zones is None:
            self._zones = await self.hass.async_add_executor_job(
                self._fetch_zones, host, port
            )

        result = self.async_create_entry(
            title=DEFAULT_ENTRY_TITLE_AU7000,
            data={
                CONF_DEVICE_TYPE: DEVICE_TYPE_AU7000,
                "host": host,
                "port": port,
                "zones": self._zones,
            },
        )
        self._schedule_companion_flow()
        return result

    async def _async_create_au7001_entry(self):
        """Create an AU7001 config entry, optionally queueing a companion flow."""
        result = self.async_create_entry(
            title=DEFAULT_ENTRY_TITLE_AU7001,
            data={
                CONF_DEVICE_TYPE: DEVICE_TYPE_AU7001,
                "location": self._ssdp_location,
                "udn": self._ssdp_udn,
                "name": DEFAULT_DEVICE_NAME_AU7001,
                "ssdp_friendly_name": self._ssdp_name,
            },
        )
        self._schedule_companion_flow()
        return result

    def _schedule_companion_flow(self) -> None:
        """Start a follow-up flow for the companion device, if requested."""
        if not self._add_companion or not self._companion_offer:
            return
        companion = self._companion_offer["companion"]

        async def _run() -> None:
            await asyncio.sleep(1)
            await self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={
                    "source": config_entries.SOURCE_USER,
                    "prefill_companion": companion,
                },
            )

        self.hass.async_create_task(_run())

    async def _async_handle_prefill_companion(self, offer: dict):
        """Jump straight to confirmation for a chained companion setup."""
        if offer["type"] == DEVICE_TYPE_AU7001:
            self._ssdp_location = offer["location"]
            self._ssdp_udn = offer["udn"]
            self._ssdp_name = offer["name"]
            return await self.async_step_ssdp_confirm()

        self._host = offer["host"]
        return await self.async_step_discovery_confirm()

    def _configured_au7000_hosts(self) -> set[str]:
        return {
            entry.data.get("host")
            for entry in self._async_current_entries()
            if entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_AU7000)
            == DEVICE_TYPE_AU7000
            and entry.data.get("host")
        }

    def _configured_au7001_udns(self) -> set[str]:
        return {
            entry.data.get("udn")
            for entry in self._async_current_entries()
            if entry.data.get(CONF_DEVICE_TYPE) == DEVICE_TYPE_AU7001
            and entry.data.get("udn")
        }

    @staticmethod
    def _label_au7000(host: str) -> str:
        return f"{DEFAULT_DEVICE_NAME_AU7000} — {host}"

    @staticmethod
    def _label_au7001(device: dict) -> str:
        return f"{DEFAULT_DEVICE_NAME_AU7001} — {device['host']}"

    @staticmethod
    def _label_manual_au7000() -> str:
        return "Enter AU7000 IP manually…"

    @staticmethod
    def _label_manual_au7001() -> str:
        return "Enter AU7001 IP manually…"

    @staticmethod
    def _host_from_location(location: str) -> str:
        return urlsplit(location).hostname or location

    # ------------------------------------------------------------------
    # Blocking discovery helpers (run in executor)
    # ------------------------------------------------------------------
    def _scan_for_all_devices(self) -> tuple[list[str], list[dict]]:
        au7000 = self._scan_for_au7000_devices()
        au7001 = self._discover_au7001_devices()
        return au7000, au7001

    def _scan_for_au7000_devices(self) -> list[str]:
        from concurrent.futures import ThreadPoolExecutor

        local_ip = self._local_ipv4()
        if not local_ip:
            _LOGGER.warning("Could not determine local IP; skipping AU7000 scan")
            return []

        prefix = local_ip.rsplit(".", 1)[0]
        candidates = [f"{prefix}.{i}" for i in range(1, 255)]
        found: list[str] = []

        def probe(ip: str) -> str | None:
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                    sock.settimeout(SCAN_TIMEOUT)
                    sock.connect((ip, DEFAULT_PORT))
                    sock.settimeout(1.0)
                    banner = sock.recv(256)
                if b"Greeting" in banner or b"Nuvo" in banner:
                    return ip
            except OSError:
                return None
            return None

        with ThreadPoolExecutor(max_workers=SCAN_WORKERS) as executor:
            for result in executor.map(probe, candidates):
                if result:
                    found.append(result)

        _LOGGER.debug("AU7000 scan found: %s", found)
        return found

    @staticmethod
    def _discover_au7001_devices() -> list[dict]:
        """Find AU7001 modules via SSDP."""
        msg = (
            "M-SEARCH * HTTP/1.1\r\n"
            "HOST: 239.255.255.250:1900\r\n"
            'MAN: "ssdp:discover"\r\n'
            f"MX: 2\r\n"
            f"ST: {NUVO_ZONE_DEVICE_TYPE}\r\n"
            "\r\n"
        ).encode()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.settimeout(SSDP_TIMEOUT)
        devices: dict[str, dict] = {}
        try:
            sock.sendto(msg, ("239.255.255.250", 1900))
            while True:
                data, _ = sock.recvfrom(65535)
                text = data.decode(errors="replace")
                location = udn = None
                for line in text.split("\r\n"):
                    lower = line.lower()
                    if lower.startswith("location:"):
                        location = line.split(":", 1)[1].strip()
                    if lower.startswith("usn:") and "uuid:" in line.lower():
                        match = re.search(r"uuid:[^\s::]+", line, re.I)
                        if match:
                            udn = match.group(0)
                if not location:
                    continue
                if not udn:
                    udn = LegrandDigitalAudioConfigFlow._fetch_udn(location)
                if not udn or location in devices:
                    continue
                name = LegrandDigitalAudioConfigFlow._fetch_friendly_name(location)
                devices[location] = {
                    "location": location,
                    "udn": udn,
                    "name": name,
                    "host": LegrandDigitalAudioConfigFlow._host_from_location(location),
                }
        except socket.timeout:
            pass
        finally:
            sock.close()
        return list(devices.values())

    @staticmethod
    def _discover_au7001_at_host(host: str) -> dict | None:
        for device in LegrandDigitalAudioConfigFlow._discover_au7001_devices():
            if device["host"] == host:
                return device
        return None

    @staticmethod
    def _fetch_friendly_name(location: str) -> str:
        try:
            with urlopen(location, timeout=FETCH_TIMEOUT) as resp:
                body = resp.read()
            root = ElementTree.fromstring(body)
            for elem in root.iter():
                if elem.tag.endswith("friendlyName") and elem.text:
                    return elem.text.strip()
        except (OSError, ElementTree.ParseError, TimeoutError):
            pass
        return "Legrand AU7001"

    @staticmethod
    def _fetch_udn(location: str) -> str | None:
        try:
            with urlopen(location, timeout=FETCH_TIMEOUT) as resp:
                body = resp.read().decode(errors="replace")
            match = re.search(r"<UDN>(.*?)</UDN>", body)
            return match.group(1).strip() if match else None
        except OSError:
            return None

    @staticmethod
    def _get_mac(host: str) -> str | None:
        try:
            from getmac import get_mac_address

            return get_mac_address(ip=host)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _local_ipv4() -> str | None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            return sock.getsockname()[0]
        except OSError:
            return None
        finally:
            sock.close()

    def _fetch_zones(self, host: str, port: int):
        """Fetch all zones from the AU7000 JSON API."""
        _LOGGER.debug("Fetching zones from %s:%s", host, port)

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(FETCH_TIMEOUT)
            sock.connect((host, port))
            sock.recv(1024)
            sock.sendall(
                (json.dumps({"ID": 3, "Service": "ListSources"}) + "\n").encode()
            )
            sources = json.loads(
                sock.recv(1024).decode("utf-8").replace("\x00", "").strip()
            )
            sock.sendall(
                (json.dumps({"ID": 4, "Service": "ListZones"}) + "\n").encode()
            )
            response = sock.recv(4096).decode("utf-8").replace("\x00", "").strip()

        devices = []
        response_json = json.loads(response)
        for zone in response_json["ZoneList"]:
            zone_id = zone.get("ZID")
            zone_name = zone.get("Name", f"Zone {zone_id}")
            if zone_id:
                devices.append(
                    {
                        "zone_id": zone_id,
                        "name": zone_name.replace(" ", "_"),
                        "sources": sources["SourceList"],
                    }
                )

        if not devices:
            raise RuntimeError("No zones found")
        return devices
