"""UPnP/SOAP control client for the Legrand/NuVo AU7001 streaming zone.

The AU7001 presents itself as a standard DLNA MediaRenderer (AVTransport +
RenderingControl) plus proprietary NuVo ContentDirectory and Zone services.
Control is SOAP over HTTP on the port advertised via SSDP.

The device rejects most ContentDirectory / AVTransport write actions until it
has been fully bound to the AU7000 (AU7001 bind LED solid white, Zone Active=1).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import socket
import time
from dataclasses import dataclass, field
from xml.etree import ElementTree
from urllib.parse import urlsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    NUVO_BROWSE_ROOT,
    NUVO_SERVICE_ZONE,
    NUVO_ZONE_DEVICE_TYPE,
    UPNP_MAX_VOLUME,
    UPNP_SERVICE_AVTRANSPORT,
    UPNP_SERVICE_CONTENT_DIRECTORY,
    UPNP_SERVICE_RENDERING,
)
from .stream_proxy import (
    Id3StreamProxy,
    async_fetch_image,
    async_local_ip_toward,
    build_id3v2,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
# Poll Gets must fail fast so config-entry unload is not blocked for 10s+.
POLL_TIMEOUT = 3
SSDP_TIMEOUT = 2.0
# Zone Get failures before marking the entity unavailable. AVTransport /
# RenderingControl blips during Music Assistant stream start must not do this.
ZONE_FAILURES_BEFORE_UNAVAILABLE = 6
# After SetAVTransportURI / SSDP port moves, Zone SOAP is often down for tens of
# seconds while the AU7001 restarts its UPnP stack. Keep the HA entity available.
STREAM_AVAILABLE_GRACE = 60.0
REDISCOVER_ATTEMPTS = 3
REDISCOVER_DELAY = 1.0
NUVO_NS = "urn:schemas.nuvotechnologies.com"
DMS_ARGUMENTS = json.dumps(
    {"dms": {"id": "home-assistant", "title": "Home Assistant"}}
)

# NuVo firmware serves these control paths; used as a fallback if the device
# description can't be parsed for some reason.
DEFAULT_CONTROL_URLS = {
    UPNP_SERVICE_AVTRANSPORT: "/AVTransport/control",
    UPNP_SERVICE_RENDERING: "/RenderingControl/control",
    UPNP_SERVICE_CONTENT_DIRECTORY: "/ContentDirectory/control",
    NUVO_SERVICE_ZONE: "/ZoneService/control",
}

# Play may return HTTP 500 while still starting transport.
_SOAP_FAULT_OK_ACTIONS = frozenset({"X_NUVO_PlayContainerURI"})

# SetAVTransportURI / Browse Action metadata may carry nested DIDL as raw XML.
# X_NUVO_PlayContainerURI is the opposite: the Digital Audio app HTML-escapes
# CurrentURIMetaData / TrackURIMetaData (&lt;DIDL-Lite…), and unescaped DIDL
# makes the DIM reject the station as "unavailable or not playable".
_RAW_SOAP_FIELDS = frozenset(
    {
        "CurrentURIMetaData",
        "TrackURIMetaData",
        "MetaData",
        "ParentMetaData",
    }
)

_DIDL_LITE_WRAPPER = (
    '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
    'xmlns:dc="http://purl.org/dc/elements/1.1/" '
    'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
    'xmlns:x="urn:schemas.nuvotechnologies.com">'
)


@dataclass
class NuvoBrowseItem:
    """One entry returned by X_NUVO_Browse2."""

    object_id: str
    title: str
    item_class: str
    didl_xml: str
    index: int = 0
    is_container: bool = False
    is_playable: bool = False


@dataclass
class NuvoBrowseResult:
    """Cached browse container context used for PlayContainerURI."""

    object_id: str
    title: str
    container_didl: str
    items: list[NuvoBrowseItem] = field(default_factory=list)


def _local(tag: str) -> str:
    """Strip an XML namespace from a tag name."""
    return tag.rsplit("}", 1)[-1]


def _parse_duration(value: str | None):
    """Convert an UPnP time string (H:MM:SS[.ms]) to whole seconds."""
    if not value or value == "NOT_IMPLEMENTED":
        return None
    try:
        parts = value.split(".")[0].split(":")
        parts = [int(p) for p in parts]
        while len(parts) < 3:
            parts.insert(0, 0)
        hours, minutes, seconds = parts[-3], parts[-2], parts[-1]
        return hours * 3600 + minutes * 60 + seconds
    except (ValueError, IndexError):
        return None


def _sanitize_didl(xml: str) -> str:
    """Escape bare ampersands in DIDL returned by the AU7001."""
    return re.sub(r"&(?!#?\w+;)", "&amp;", xml)


def _parse_didl(xml: str) -> ElementTree.Element | None:
    """Parse DIDL-Lite, tolerating minor malformations from the device."""
    if not xml or not xml.strip():
        return None
    text = html.unescape(xml)
    try:
        return ElementTree.fromstring(text)
    except ElementTree.ParseError:
        try:
            return ElementTree.fromstring(_sanitize_didl(text))
        except ElementTree.ParseError as e:
            _LOGGER.debug("DIDL parse failed: %s", e)
            return None


def _extract_raw_elements(raw_result: str) -> dict[str, str]:
    """Map object id to the raw item/container XML from a Browse2 Result."""
    elements: dict[str, str] = {}
    for tag in ("item", "container"):
        pattern = re.compile(
            rf'(<{tag}\s+id="([^"]+)"[\s\S]*?</{tag}>)',
            re.MULTILINE,
        )
        for match in pattern.finditer(raw_result):
            elements[match.group(2)] = match.group(1)
    return elements


def _element_xml(elem: ElementTree.Element) -> str:
    """Serialize a single DIDL element back to XML."""
    return ElementTree.tostring(elem, encoding="unicode")


def _as_didl_lite(fragment: str) -> str:
    """Wrap a bare <item>/<container> fragment in DIDL-Lite if needed.

    PlayContainerURI requires TrackURIMetaData to be a full DIDL-Lite document
    (matching the Digital Audio app), not a naked browse Result child.
    """
    text = (fragment or "").strip()
    if not text:
        return ""
    if text.lstrip().startswith("<DIDL-Lite") or text.lstrip().startswith("<ns0:DIDL"):
        return text
    return f"{_DIDL_LITE_WRAPPER}{text}</DIDL-Lite>"


def _parse_group_id(master_group: str | None) -> str:
    """Extract the group id string from the Zone MasterGroup JSON field."""
    if not master_group:
        return ""
    try:
        data = json.loads(master_group)
        return str(data.get("id", ""))
    except (json.JSONDecodeError, TypeError):
        return ""


def _ssdp_discover_au7001() -> list[dict[str, str]]:
    """Blocking SSDP M-SEARCH for AU7001 Zone devices."""
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {NUVO_ZONE_DEVICE_TYPE}\r\n"
        "\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(SSDP_TIMEOUT)
    devices: dict[str, dict[str, str]] = {}
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
                if lower.startswith("usn:") and "uuid:" in lower:
                    match = re.search(r"uuid:[^\s::]+", line, re.I)
                    if match:
                        udn = match.group(0)
            if not location or location in devices:
                continue
            host = urlsplit(location).hostname or ""
            devices[location] = {
                "location": location,
                "udn": udn or "",
                "host": host,
            }
    except (TimeoutError, socket.timeout):
        pass
    except OSError as err:
        _LOGGER.debug("SSDP discover failed: %s", err)
    finally:
        sock.close()
    return list(devices.values())


class NuvoUpnpZone:
    """Controls a single AU7001 streaming zone over UPnP/SOAP."""

    def __init__(self, hass, location: str, udn: str, name: str):
        self._hass = hass
        self._location = location
        self._udn = udn
        self._name = name

        self._base: str | None = None
        self._control_urls: dict[str, str] = {}
        self._available = False
        self._zone_failures = 0
        self._rediscovering = False
        self._closed = False
        self._hold_available_until = 0.0

        # Polled state.
        self.state = "idle"  # idle | playing | paused
        self.active = False
        self.connecting = False
        self.system_id: str | None = None
        self.member_id: str | None = None
        self.zone_title: str | None = None
        self.volume_level: float | None = None
        self.is_muted = False
        self.media_title: str | None = None
        self.media_artist: str | None = None
        self.media_album: str | None = None
        self.media_image_url: str | None = None
        self.media_duration: int | None = None
        self.media_position: int | None = None
        self.power_state: str | None = None
        self.model: str | None = None
        self.group_id: str = ""

        # Browse / play context.
        self._queue_id: str | None = None
        self._browse_context: NuvoBrowseResult | None = None
        self._items_by_id: dict[str, NuvoBrowseItem] = {}
        self._waiting_for_active = False
        self._stream_proxy: Id3StreamProxy | None = None
        self._stream_metadata: dict[str, str | None] | None = None
        self.last_browse_error: str | None = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def is_active(self) -> bool:
        """True when the AU7001 is fully bound (Zone Active=1)."""
        return self.active

    @property
    def bind_status(self) -> str:
        """Human-readable bind state for entity attributes / diagnostics."""
        if not self._available:
            return "unavailable"
        if self.active:
            return "bound"
        if self.connecting:
            return "connecting"
        if self.system_id:
            return "awaiting_button"
        return "unbound"

    @property
    def bind_hint(self) -> str:
        """Short guidance for the current bind_status."""
        status = self.bind_status
        if status == "bound":
            return "Bound (Active=1). Streaming is available."
        if status == "connecting":
            return "Bind in progress. If prompted, press the AU7001 bind button."
        if status == "awaiting_button":
            return (
                "System ID created. Press the physical bind button on the AU7001 "
                "until the LED is solid white."
            )
        if status == "unbound":
            return (
                "Not bound. Use Start bind (or the Digital Audio app), then press "
                "the AU7001 bind button until the LED is solid white."
            )
        return "AU7001 is unreachable. Check power and network."

    @property
    def udn(self) -> str:
        return self._udn

    @property
    def name(self) -> str:
        return self._name

    @property
    def host(self) -> str:
        """IP address from the SSDP location URL."""
        return urlsplit(self._location).hostname or ""

    @property
    def configuration_url(self) -> str | None:
        """Base URL for the UPnP device description."""
        if not self._location:
            return None
        split = urlsplit(self._location)
        return f"{split.scheme}://{split.netloc}"

    def update_location(self, location: str) -> None:
        """Update the SSDP location (e.g. after the device rebooted on a new port)."""
        if location and location != self._location:
            _LOGGER.info("[%s] Location updated: %s → %s", self._name, self._location, location)
            self._location = location
            self._base = None  # force re-resolve of control URLs
            # Subscribe queues die with the old UPnP HTTP port.
            self._queue_id = None
            self._browse_context = None
            self._items_by_id.clear()
            # Port churn means Zone Get will fail briefly; do not flap available.
            self._hold_available()

    def _hold_available(self, seconds: float = STREAM_AVAILABLE_GRACE) -> None:
        """Keep the entity available through known UPnP stack resets."""
        until = time.monotonic() + seconds
        if until > self._hold_available_until:
            self._hold_available_until = until
        self._available = True
        self._zone_failures = 0

    def _note_zone_failure(self) -> None:
        """Count a Zone Get failure toward unavailable, with stream grace."""
        if time.monotonic() < self._hold_available_until:
            _LOGGER.debug(
                "[%s] Zone Get failed during availability grace; keeping available",
                self._name,
            )
            return
        # An active ID3 proxy means Music Assistant audio is still flowing.
        if self._stream_proxy is not None:
            _LOGGER.debug(
                "[%s] Zone Get failed while stream proxy is active; keeping available",
                self._name,
            )
            return
        self._zone_failures += 1
        if self._zone_failures >= ZONE_FAILURES_BEFORE_UNAVAILABLE:
            self._available = False

    async def async_close(self):
        """Release browse state (no persistent socket for UPnP)."""
        # Stop polls/rediscovery immediately so config-entry unload cannot hang
        # in unload_in_progress while SOAP/SSDP work is still in flight.
        self._closed = True
        self._waiting_for_active = False
        self._rediscovering = False
        await self._async_stop_stream_proxy()
        self._queue_id = None
        self._browse_context = None
        self._items_by_id.clear()
        self._stream_metadata = None

    # ------------------------------------------------------------------
    # Description / control-URL resolution
    # ------------------------------------------------------------------
    async def _async_resolve(self) -> bool:
        """Fetch the device description and resolve control URLs."""
        if self._closed:
            return False
        split = urlsplit(self._location)
        self._base = f"{split.scheme}://{split.netloc}"
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(self._location, timeout=POLL_TIMEOUT) as resp:
                resp.raise_for_status()
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.debug("[%s] Description fetch failed: %s", self._name, e)
            self._base = None
            return False

        control_urls = dict(DEFAULT_CONTROL_URLS)
        try:
            root = ElementTree.fromstring(body)
            for service in root.iter():
                if _local(service.tag) != "service":
                    continue
                svc_type = control = None
                for child in service:
                    if _local(child.tag) == "serviceType":
                        svc_type = (child.text or "").strip()
                    elif _local(child.tag) == "controlURL":
                        control = (child.text or "").strip()
                if svc_type and control:
                    control_urls[svc_type] = control
        except ElementTree.ParseError as e:
            _LOGGER.debug(
                "[%s] Could not parse description (%s); using default control URLs",
                self._name,
                e,
            )

        self._control_urls = control_urls
        return True

    async def _async_rediscover_location(self) -> bool:
        """SSDP rediscovery when the AU7001 moves to a new HTTP control port.

        Music Assistant stream start often resets the device's UPnP stack; the
        description URL port changes and the cached location goes stale.
        """
        if self._closed or self._rediscovering:
            return False
        self._rediscovering = True
        try:
            host = self.host
            udn = (self._udn or "").lower()
            for attempt in range(REDISCOVER_ATTEMPTS):
                if self._closed:
                    return False
                devices = await self._hass.async_add_executor_job(
                    _ssdp_discover_au7001
                )
                match = None
                for device in devices:
                    device_udn = (device.get("udn") or "").lower()
                    if udn and device_udn and device_udn == udn:
                        match = device
                        break
                    if host and device.get("host") == host:
                        match = device
                if match:
                    self.update_location(match["location"])
                    if await self._async_resolve():
                        return True
                if attempt + 1 < REDISCOVER_ATTEMPTS:
                    await asyncio.sleep(REDISCOVER_DELAY)
            _LOGGER.debug(
                "[%s] SSDP rediscovery found no matching AU7001 after %s attempts",
                self._name,
                REDISCOVER_ATTEMPTS,
            )
            return False
        finally:
            self._rediscovering = False

    def _control_url(self, service_type: str) -> str:
        path = self._control_urls.get(
            service_type, DEFAULT_CONTROL_URLS.get(service_type, "")
        )
        if path.startswith("http"):
            return path
        if not path.startswith("/"):
            path = "/" + path
        return f"{self._base}{path}"

    # ------------------------------------------------------------------
    # SOAP
    # ------------------------------------------------------------------
    async def _soap(
        self,
        service_type: str,
        action: str,
        args: dict | None = None,
        raw_fields: frozenset[str] | None = None,
        *,
        timeout: float | None = None,
    ):
        """Invoke a SOAP action; return a dict of response fields or None.

        Connection errors clear the cached base URL so the next call re-resolves
        the description. Only Zone failures affect entity availability — the
        AU7001 routinely resets AVTransport/RenderingControl sockets when Music
        Assistant starts a stream, and those blips must not mark the player
        unavailable.
        """
        if self._closed:
            return None
        if self._base is None and not await self._async_resolve():
            return None
        if self._closed:
            return None

        # Use `is None` so callers can pass frozenset() to force escaping all
        # fields (empty set is falsy and must not fall back to _RAW_SOAP_FIELDS).
        raw = _RAW_SOAP_FIELDS if raw_fields is None else raw_fields
        parts = []
        for key, value in (args or {}).items():
            text = str(value)
            if key in raw:
                parts.append(f"<{key}>{text}</{key}>")
            else:
                parts.append(f"<{key}>{html.escape(text)}</{key}>")
        args_xml = "".join(parts)

        envelope = (
            '<?xml version="1.0"?>'
            '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
            's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">'
            f'<s:Body><u:{action} xmlns:u="{service_type}">{args_xml}'
            f"</u:{action}></s:Body></s:Envelope>"
        )
        headers = {
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPACTION": f'"{service_type}#{action}"',
        }
        if timeout is None:
            timeout = POLL_TIMEOUT if action.startswith("Get") else REQUEST_TIMEOUT
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                self._control_url(service_type),
                data=envelope.encode("utf-8"),
                headers=headers,
                timeout=timeout,
            ) as resp:
                text = await resp.text()
                if resp.status >= 400:
                    fault = ""
                    if "errorDescription" in text:
                        match = re.search(
                            r"<errorDescription>(.*?)</errorDescription>",
                            text,
                            re.DOTALL,
                        )
                        if match:
                            fault = match.group(1).strip()
                    _LOGGER.debug(
                        "[%s] SOAP %s HTTP %s%s",
                        self._name,
                        action,
                        resp.status,
                        f": {fault}" if fault else "",
                    )
                    if action in _SOAP_FAULT_OK_ACTIONS:
                        return {}
                    return None
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.warning("[%s] SOAP %s failed: %s", self._name, action, e)
            # Force description re-resolve; do not flip availability here.
            self._base = None
            return None

        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as e:
            _LOGGER.warning("[%s] SOAP %s bad response: %s", self._name, action, e)
            return None

        result: dict[str, str] = {}
        for elem in root.iter():
            if _local(elem.tag) == f"{action}Response":
                for child in elem:
                    result[_local(child.tag)] = child.text or ""
                break
        return result

    # ------------------------------------------------------------------
    # Browse / play (ContentDirectory + AVTransport)
    # ------------------------------------------------------------------
    async def _async_ensure_queue(self, *, force_new: bool = False) -> str | None:
        """Create or reuse the ContentDirectory subscription queue."""
        if not self.active:
            _LOGGER.warning(
                "[%s] AU7001 is inactive; complete bind (solid white LED) first",
                self._name,
            )
            return None
        if self._queue_id and not force_new:
            return self._queue_id

        self._queue_id = None
        resp = await self._soap(
            UPNP_SERVICE_CONTENT_DIRECTORY, "X_NUVO_CreateSubscribeQueue", {}
        )
        if not resp or not resp.get("QueueID"):
            return None
        self._queue_id = resp["QueueID"]
        return self._queue_id

    async def _async_browse2(
        self, object_id: str, browse_flag: str, queue_id: str
    ) -> dict[str, str] | None:
        """Invoke X_NUVO_Browse2 once."""
        return await self._soap(
            UPNP_SERVICE_CONTENT_DIRECTORY,
            "X_NUVO_Browse2",
            {
                "ObjectID": object_id,
                "BrowseFlag": browse_flag,
                "GroupID": self.group_id,
                "MetaData": "",
                "ParentMetaData": "",
                "Index": 0,
                "Filter": "*",
                "StartingIndex": 0,
                "RequestedCount": 0,
                "SubscribeQueueID": queue_id,
                "Arguments": DMS_ARGUMENTS,
            },
        )

    async def async_browse(
        self,
        object_id: str = NUVO_BROWSE_ROOT,
        browse_flag: str = "BrowseDirectChildren",
    ) -> NuvoBrowseResult | None:
        """Browse the on-device music menu via X_NUVO_Browse2."""
        self.last_browse_error = None
        if not self.active:
            self.last_browse_error = (
                "AU7001 is inactive. Complete bind (solid white LED) before browsing."
            )
            return None

        queue_id = await self._async_ensure_queue()
        if not queue_id:
            self.last_browse_error = (
                "Could not create a ContentDirectory subscribe queue. "
                "Stop Music Assistant playback and retry, or reload the AU7001 entry."
            )
            return None

        resp = await self._async_browse2(object_id, browse_flag, queue_id)
        if not resp:
            # Stale queue IDs are common after the AU7001 moves UPnP ports.
            _LOGGER.debug(
                "[%s] Browse2 failed for %s; recreating subscribe queue",
                self._name,
                object_id,
            )
            queue_id = await self._async_ensure_queue(force_new=True)
            if queue_id:
                resp = await self._async_browse2(object_id, browse_flag, queue_id)

        if not resp:
            self.last_browse_error = (
                f"Browse failed for {object_id}. The AU7001 may be busy with "
                "Music Assistant streaming — stop MA playback and try again."
            )
            return None

        result = self._parse_browse_result(object_id, resp)
        if result is None:
            self.last_browse_error = f"Browse response for {object_id} could not be parsed."
            return None
        self._browse_context = result
        self._items_by_id = {item.object_id: item for item in result.items}
        return result

    def _parse_browse_result(
        self, object_id: str, resp: dict[str, str]
    ) -> NuvoBrowseResult | None:
        """Parse a Browse2 response into structured items."""
        raw_result = html.unescape(resp.get("Result", ""))
        raw_container = html.unescape(resp.get("ContainerProperties", ""))
        if not raw_result.strip():
            return NuvoBrowseResult(object_id, object_id, raw_container)

        title = object_id
        items: list[NuvoBrowseItem] = []
        raw_elements = _extract_raw_elements(raw_result)
        root = _parse_didl(raw_result)
        if root is None:
            _LOGGER.warning("[%s] Browse parse failed for %s", self._name, object_id)
            return None

        index = 0
        for elem in root:
            tag = _local(elem.tag)
            if tag not in ("item", "container"):
                continue
            oid = elem.get("id", "")
            item_title = ""
            item_class = ""
            for child in elem:
                ctag = _local(child.tag)
                if ctag == "title" and child.text:
                    item_title = child.text.strip()
                elif ctag == "class" and child.text:
                    item_class = child.text.strip()

            if not oid or item_title.lower() in ("cancel",):
                continue

            is_container = tag == "container" or "container" in item_class
            # Pandora stations are <item> rows under pandora:StationList; class is
            # usually audioItem/audioBroadcast, but treat known service ids as
            # playable even when the firmware omits/oddly labels upnp:class.
            is_playable = tag == "item" and (
                "audioItem" in item_class
                or "audioBroadcast" in item_class
                or oid.startswith(
                    ("pandora:", "spotify:", "siriusxm:", "tunein:", "nuvo:")
                )
            )
            browse_item = NuvoBrowseItem(
                object_id=oid,
                title=item_title or oid,
                item_class=item_class,
                didl_xml=raw_elements.get(oid) or _element_xml(elem),
                index=index,
                is_container=is_container,
                is_playable=is_playable,
            )
            items.append(browse_item)
            index += 1

        if raw_container.strip():
            croot = _parse_didl(raw_container)
            if croot is not None:
                for elem in croot.iter():
                    if _local(elem.tag) == "title" and elem.text:
                        title = elem.text.strip()
                        break

        return NuvoBrowseResult(
            object_id=object_id,
            title=title,
            container_didl=raw_container,
            items=items,
        )

    async def async_play_browse_item(self, item_id: str) -> bool:
        """Start playback of a browsed item via X_NUVO_PlayContainerURI."""
        if not self.active:
            _LOGGER.warning("[%s] Cannot play while AU7001 is inactive", self._name)
            return False

        item = self._items_by_id.get(item_id)
        context = self._browse_context
        if item is None or context is None:
            # Media browser may call play after the in-memory browse cache was
            # cleared (UPnP port change). Re-browse the parent container once.
            parent = item_id.rsplit("/", 1)[0] if "/" in item_id else None
            if parent:
                _LOGGER.debug(
                    "[%s] Browse cache miss for %s; re-browsing %s",
                    self._name,
                    item_id,
                    parent,
                )
                await self.async_browse(parent)
                item = self._items_by_id.get(item_id)
                context = self._browse_context
        if item is None or context is None:
            _LOGGER.warning(
                "[%s] Unknown browse item %s; browse the parent container first",
                self._name,
                item_id,
            )
            return False
        if not (context.container_didl or "").strip():
            _LOGGER.warning(
                "[%s] Missing ContainerProperties for %s; re-browsing parent",
                self._name,
                item_id,
            )
            parent = context.object_id or (
                item_id.rsplit("/", 1)[0] if "/" in item_id else None
            )
            if parent:
                await self.async_browse(parent)
                item = self._items_by_id.get(item_id) or item
                context = self._browse_context or context
        if not (context.container_didl or "").strip():
            _LOGGER.error(
                "[%s] Cannot play %s without ContainerProperties",
                self._name,
                item_id,
            )
            return False

        # Clear Music Assistant / prior transport before Pandora/NuVo play.
        await self._async_stop_stream_proxy()
        if self.state != "idle":
            await self.async_stop()
            await asyncio.sleep(0.75)

        track_uri = item_id if item_id.startswith("nuvo:") else f"nuvo:{item_id}"
        track_meta = _as_didl_lite(item.didl_xml)
        container_meta = _as_didl_lite(context.container_didl)
        _LOGGER.info(
            "[%s] PlayContainerURI %s (index=%s)",
            self._name,
            item_id,
            item.index + 1,
        )
        _LOGGER.debug(
            "[%s] PlayContainerURI meta lengths container=%s track=%s",
            self._name,
            len(container_meta),
            len(track_meta),
        )
        # Escape DIDL fields (raw_fields=empty). Nested unescaped DIDL is
        # rejected as "unavailable or not playable by Legrand DIM".
        resp = await self._soap(
            UPNP_SERVICE_AVTRANSPORT,
            "X_NUVO_PlayContainerURI",
            {
                "InstanceID": 0,
                "CurrentURI": "",
                "CurrentURIMetaData": container_meta,
                "TrackURI": track_uri,
                "TrackURIMetaData": track_meta,
                "StartingIndex": item.index + 1,
                "UpdateID": -1,
            },
            raw_fields=frozenset(),
        )

        self.media_title = item.title
        if item_id.startswith("pandora:"):
            self.media_artist = "pandora"
        self._hold_available()

        for _ in range(10):
            await self._async_refresh_transport()
            if self.state == "playing":
                return True
            await asyncio.sleep(0.75)

        if resp is not None:
            # HTTP 200 or the known fault-OK path; device may still be buffering.
            self.state = "playing"
            _LOGGER.info(
                "[%s] PlayContainerURI accepted for %s; transport not confirmed yet",
                self._name,
                item_id,
            )
            return True

        _LOGGER.error("[%s] PlayContainerURI failed for %s", self._name, item_id)
        return False

    # ------------------------------------------------------------------
    # Bind helpers (captured from Digital Audio app + button)
    # ------------------------------------------------------------------
    def _apply_zone_get(self, zone: dict[str, str]) -> None:
        """Apply Zone Get / SystemCreate fields to polled state."""
        self._available = True
        self.power_state = zone.get("PowerState")
        self.model = zone.get("Model")
        self.active = zone.get("Active") == "1"
        self.connecting = zone.get("Connecting") == "1"
        system_id = (zone.get("SystemID") or "").strip()
        self.system_id = system_id or None
        member_id = (zone.get("MemberID") or "").strip()
        self.member_id = member_id or None
        title = (zone.get("Title") or "").strip()
        self.zone_title = title or None
        self.group_id = _parse_group_id(zone.get("MasterGroup"))
        if not self.active:
            self._queue_id = None

    async def async_system_create(self) -> str | None:
        """Create a new NuVo system ID (app Bind Digital Source step).

        Mirrors the Digital Audio app SOAP call:
        Zone#SystemCreate with an empty primaryGwMac. Returns the new SystemID
        on success, or None on failure. Does not complete bind by itself —
        Active=1 still requires the physical AU7001 bind button.
        """
        resp = await self._soap(
            NUVO_SERVICE_ZONE,
            "SystemCreate",
            {"primaryGwMac": ""},
        )
        if resp is None:
            return None
        new_id = (resp.get("newSystemID") or "").strip()
        if new_id:
            self.system_id = new_id
        await self.async_update_zone_only()
        return self.system_id

    async def async_attempt_bind(self) -> dict[str, str | bool | None]:
        """Start the software half of bind; report what the user must do next."""
        await self.async_update_zone_only()
        if self.active:
            return {
                "status": "already_bound",
                "system_id": self.system_id,
                "message": self.bind_hint,
            }

        new_id = await self.async_system_create()
        if new_id is None and not self.system_id:
            return {
                "status": "failed",
                "system_id": None,
                "message": (
                    "SystemCreate failed. Confirm the AU7001 is online, then retry "
                    "or use Bind Digital Source in the Digital Audio app."
                ),
            }

        await self.async_update_zone_only()
        if self.active:
            return {
                "status": "bound",
                "system_id": self.system_id,
                "message": self.bind_hint,
            }
        return {
            "status": "press_button",
            "system_id": self.system_id,
            "message": self.bind_hint,
        }

    async def async_update_zone_only(self) -> bool:
        """Refresh Zone Get bind fields without transport/volume polling."""
        if self._closed:
            return False
        zone = await self._soap(NUVO_SERVICE_ZONE, "Get")
        if self._closed:
            return False
        if zone is None:
            if await self._async_rediscover_location():
                zone = await self._soap(NUVO_SERVICE_ZONE, "Get")
        if self._closed:
            return False
        if zone is None:
            self._note_zone_failure()
            return False
        self._zone_failures = 0
        self._apply_zone_get(zone)
        return True

    async def _async_wait_for_active(self, attempts: int = 8, delay: float = 1.0):
        """Poll briefly after reconnect; firmware often restores Active itself."""
        if self._waiting_for_active or self._closed:
            return
        self._waiting_for_active = True
        try:
            for _ in range(attempts):
                if self._closed or self.active or not self._available:
                    return
                await asyncio.sleep(delay)
                await self.async_update_zone_only()
                if self.active:
                    _LOGGER.info(
                        "[%s] Bind restored after reconnect (SystemID=%s)",
                        self._name,
                        self.system_id,
                    )
                    return
            if not self.active:
                _LOGGER.warning(
                    "[%s] Still unbound after reconnect: %s",
                    self._name,
                    self.bind_hint,
                )
        finally:
            self._waiting_for_active = False

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------
    async def _async_refresh_transport(self) -> None:
        """Best-effort transport/volume/mute/position refresh.

        Used after Music Assistant stream start so confirmation polling does not
        hammer Zone Get (and mark the entity unavailable) while UPnP restarts.
        """
        if self._closed:
            return
        transport = await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "GetTransportInfo", {"InstanceID": 0}
        )
        if self._closed:
            return
        if transport is not None:
            tstate = transport.get("CurrentTransportState", "")
            if tstate == "PLAYING":
                self.state = "playing"
            elif tstate == "PAUSED_PLAYBACK":
                self.state = "paused"
            elif (
                self._stream_proxy is not None
                or time.monotonic() < self._hold_available_until
            ):
                # UPnP often reports STOPPED/TRANSITIONING during the port reset
                # even though the HTTP stream is already playing.
                if self.state not in ("playing", "paused"):
                    self.state = "playing"
            else:
                self.state = "idle"

        volume = await self._soap(
            UPNP_SERVICE_RENDERING,
            "GetVolume",
            {"InstanceID": 0, "Channel": "Master"},
        )
        if volume and volume.get("CurrentVolume", "").isdigit():
            self.volume_level = int(volume["CurrentVolume"]) / UPNP_MAX_VOLUME

        mute = await self._soap(
            UPNP_SERVICE_RENDERING,
            "GetMute",
            {"InstanceID": 0, "Channel": "Master"},
        )
        if mute:
            self.is_muted = mute.get("CurrentMute") in ("1", "true", "True")

    async def async_update(self):
        """Refresh all polled state from the device.

        Zone Get is the availability heartbeat. Transport/volume/mute/position
        are best-effort — failures during Music Assistant stream start are
        common and must not take the entity offline.
        """
        if self._closed:
            return
        was_available = self._available
        zone = await self._soap(NUVO_SERVICE_ZONE, "Get")
        if self._closed:
            return
        if zone is None:
            if await self._async_rediscover_location():
                zone = await self._soap(NUVO_SERVICE_ZONE, "Get")
        if self._closed:
            return
        if zone is None:
            self._note_zone_failure()
            # Still try transport so now-playing can recover on the new port.
            await self._async_refresh_transport()
            return

        self._zone_failures = 0
        self._apply_zone_get(zone)
        if not was_available and not self.active:
            # Power-loss capture: Active often returns within a few seconds
            # with no SOAP bind traffic. Wait briefly before declaring unbound.
            self._hass.async_create_task(self._async_wait_for_active())

        await self._async_refresh_transport()

        position = await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "GetPositionInfo", {"InstanceID": 0}
        )
        if position:
            self.media_duration = _parse_duration(position.get("TrackDuration"))
            self.media_position = _parse_duration(position.get("RelTime"))
            self._parse_metadata(position.get("TrackMetaData", ""))

    def _parse_metadata(self, didl: str):
        """Extract title/artist/album/art from DIDL-Lite metadata.

        Pandora/NuVo put the service name in dc:creator (e.g. "pandora") and
        the real performer in upnp:artist / x_nuvo_nsdk.metaData — prefer those.
        """
        if not didl or not didl.strip():
            # Keep Music Assistant / stream-proxy metadata when the device
            # has not published DIDL yet (common right after SetAVTransportURI).
            if self._stream_metadata:
                self.media_title = self._stream_metadata.get("title")
                self.media_artist = self._stream_metadata.get("artist")
                self.media_album = self._stream_metadata.get("album")
                self.media_image_url = self._stream_metadata.get("image_url")
            return
        root = _parse_didl(didl)
        if root is None:
            return
        self.media_title = None
        self.media_artist = None
        self.media_album = None
        self.media_image_url = None

        title = None
        artist = None
        creator = None
        album = None
        image = None
        nsdk_raw = None

        for elem in root.iter():
            tag = _local(elem.tag)
            text = (elem.text or "").strip()
            if tag == "x_nuvo_nsdk" and text:
                nsdk_raw = text
                continue
            if not text:
                continue
            if tag == "title" and title is None:
                # First dc:title is the track; nested containers may add more.
                title = text
            elif tag == "artist":
                artist = text
            elif tag == "creator":
                creator = text
            elif tag == "album":
                album = text
            elif tag in ("albumArtURI", "icon") and text.startswith("http"):
                image = image or text

        nsdk_artist = nsdk_album = nsdk_title = nsdk_icon = None
        if nsdk_raw:
            try:
                nsdk = json.loads(html.unescape(nsdk_raw))
            except (json.JSONDecodeError, TypeError):
                nsdk = None
            if isinstance(nsdk, dict):
                nsdk_title = nsdk.get("title") or None
                nsdk_icon = nsdk.get("icon") if isinstance(nsdk.get("icon"), str) else None
                meta = (nsdk.get("mediaData") or {}).get("metaData") or {}
                if isinstance(meta, dict):
                    nsdk_artist = meta.get("artist") or None
                    nsdk_album = meta.get("album") or None

        self.media_title = title or nsdk_title
        # Never treat the streaming-service id (dc:creator) as the performer.
        service_ids = {"pandora", "spotify", "siriusxm", "tunein", "http", "nuvo"}
        if artist:
            self.media_artist = artist
        elif nsdk_artist:
            self.media_artist = nsdk_artist
        elif creator and creator.lower() not in service_ids:
            self.media_artist = creator

        self.media_album = album or nsdk_album
        if image:
            self.media_image_url = image
        elif isinstance(nsdk_icon, str) and nsdk_icon.startswith("http"):
            self.media_image_url = nsdk_icon

        # Fill gaps from the stream we pushed (Music Assistant metadata).
        if self._stream_metadata:
            self.media_title = self.media_title or self._stream_metadata.get("title")
            self.media_artist = self.media_artist or self._stream_metadata.get("artist")
            self.media_album = self.media_album or self._stream_metadata.get("album")
            self.media_image_url = self.media_image_url or self._stream_metadata.get(
                "image_url"
            )

    # ------------------------------------------------------------------
    # Transport commands
    # ------------------------------------------------------------------
    async def async_play(self):
        await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "Play", {"InstanceID": 0, "Speed": 1}
        )

    async def async_pause(self):
        await self._soap(UPNP_SERVICE_AVTRANSPORT, "Pause", {"InstanceID": 0})

    async def async_stop(self):
        await self._soap(UPNP_SERVICE_AVTRANSPORT, "Stop", {"InstanceID": 0})
        await self._async_stop_stream_proxy()
        self._stream_metadata = None

    async def async_next(self):
        await self._soap(UPNP_SERVICE_AVTRANSPORT, "Next", {"InstanceID": 0})

    async def async_previous(self):
        await self._soap(UPNP_SERVICE_AVTRANSPORT, "Previous", {"InstanceID": 0})

    async def async_set_volume(self, level: float):
        desired = max(0, min(UPNP_MAX_VOLUME, round(level * UPNP_MAX_VOLUME)))
        await self._soap(
            UPNP_SERVICE_RENDERING,
            "SetVolume",
            {"InstanceID": 0, "Channel": "Master", "DesiredVolume": desired},
        )

    async def async_set_mute(self, mute: bool):
        await self._soap(
            UPNP_SERVICE_RENDERING,
            "SetMute",
            {"InstanceID": 0, "Channel": "Master", "DesiredMute": 1 if mute else 0},
        )

    async def _async_advertise_host(
        self, device_host: str | None, device_port: int
    ) -> str | None:
        """Pick a LAN IP the AU7001 can use to fetch the ID3 proxy."""
        # Prefer HA's network helper when available (correct under Docker/HAOS).
        try:
            from homeassistant.components.network import async_get_source_ip

            if device_host:
                ip = await async_get_source_ip(self._hass, target_ip=device_host)
                if ip:
                    return ip
        except Exception as err:  # noqa: BLE001 - optional HA API
            _LOGGER.debug("[%s] async_get_source_ip unavailable: %s", self._name, err)

        if device_host:
            return await async_local_ip_toward(device_host, device_port)
        return None

    async def _async_stop_stream_proxy(self) -> None:
        proxy = self._stream_proxy
        self._stream_proxy = None
        if proxy is not None:
            await proxy.stop()

    async def _async_prepare_play_uri(
        self,
        uri: str,
        title: str,
        artist: str | None,
        album: str | None,
        image_url: str | None,
    ) -> str:
        """Return the URL the AU7001 should fetch, with ID3 tags if needed.

        Firmware ignores DIDL for HTTP streams and reads ID3 from the audio
        bytes instead. When we have now-playing fields, proxy the upstream
        URL and prepend an ID3v2 tag.
        """
        await self._async_stop_stream_proxy()
        self._stream_metadata = {
            "title": title,
            "artist": artist,
            "album": album,
            "image_url": image_url,
        }
        if not any((title and title != "Stream", artist, album)):
            return uri

        split = urlsplit(self._location)
        device_host = split.hostname
        device_port = split.port or (443 if split.scheme == "https" else 80)
        advertise_host = await self._async_advertise_host(device_host, device_port)
        if not advertise_host:
            _LOGGER.warning(
                "[%s] Could not determine local IP for ID3 proxy; "
                "AU7001 may show Unknown artist/album",
                self._name,
            )
            return uri

        image = None
        image_mime = "image/jpeg"
        if image_url and image_url.startswith(("http://", "https://")):
            session = async_get_clientsession(self._hass)
            fetched = await async_fetch_image(session, image_url)
            if fetched:
                image, image_mime = fetched

        id3 = build_id3v2(
            title=title or None,
            artist=artist,
            album=album,
            image=image,
            image_mime=image_mime,
        )
        if not id3:
            return uri

        session = async_get_clientsession(self._hass)
        proxy = Id3StreamProxy(session, uri, id3, advertise_host)
        try:
            play_url = await proxy.start()
        except OSError as err:
            _LOGGER.warning("[%s] ID3 stream proxy failed to start: %s", self._name, err)
            return uri

        self._stream_proxy = proxy
        _LOGGER.info(
            "[%s] Proxying stream with ID3 tags (title=%s artist=%s album=%s)",
            self._name,
            title,
            artist or "-",
            album or "-",
        )
        return play_url

    def _build_stream_didl(
        self,
        uri: str,
        title: str,
        artist: str | None,
        album: str | None,
        image_url: str | None,
        protocol: str,
    ) -> str:
        """Build DIDL matching the shape Pandora now-playing uses on the AU7001."""
        description = f"{album or ''} - {artist or ''}"
        nsdk = {
            "mediaData": {
                "metaData": {
                    **({"artist": artist} if artist else {}),
                    **({"album": album} if album else {}),
                    "serviceID": "http",
                },
                "resources": [{"mimeType": "audio/mpeg", "uri": uri}],
            },
            "title": title,
            "description": description,
            "type": "audio",
        }
        if image_url:
            nsdk["icon"] = image_url
            nsdk["mediaData"]["albumArtURI"] = image_url

        nsdk_xml = html.escape(json.dumps(nsdk, separators=(",", ":")), quote=True)
        parts = [
            f"<dc:title>{html.escape(title)}</dc:title>",
            "<dc:creator>http</dc:creator>",
            f'<res protocolInfo="{protocol}">{uri}</res>',
            "<upnp:class>object.item.audioItem</upnp:class>",
            f"<dc:description>{html.escape(description)}</dc:description>",
        ]
        if artist:
            parts.append(f"<upnp:artist>{html.escape(artist)}</upnp:artist>")
        if album:
            parts.append(f"<upnp:album>{html.escape(album)}</upnp:album>")
        if image_url:
            parts.append(f"<upnp:icon>{html.escape(image_url)}</upnp:icon>")
        parts.append(f"<x:x_nuvo_nsdk>{nsdk_xml}</x:x_nuvo_nsdk>")

        return (
            '<?xml version="1.0"?>'
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/" '
            'xmlns:x="urn:schemas.nuvotechnologies.com">'
            '<item id="" restricted="1">'
            f"{''.join(parts)}"
            "</item></DIDL-Lite>"
        )

    async def async_play_uri(
        self,
        uri: str,
        title: str = "Stream",
        *,
        artist: str | None = None,
        album: str | None = None,
        image_url: str | None = None,
    ) -> bool:
        """Stream an HTTP/HTTPS URL via standard DLNA SetAVTransportURI.

        Requires the AU7001 to be fully bound (Zone Active=1). Used by Music
        Assistant and other callers that push a reachable audio stream URL.

        The AU7001 reads now-playing info from ID3 tags in the audio stream
        (not from DIDL). When title/artist/album are provided, we proxy the
        stream and prepend an ID3v2 tag so the Legrand app can display them.
        """
        if not self.active:
            _LOGGER.warning(
                "[%s] Cannot stream URL while AU7001 is inactive", self._name
            )
            return False

        uri = uri.strip()
        if self.state != "idle":
            # Music Assistant skips stop when paused; clear Pandora/nuvo first.
            await self.async_stop()
            await asyncio.sleep(0.5)

        play_uri = await self._async_prepare_play_uri(
            uri, title, artist, album, image_url
        )

        ext = play_uri.rsplit(".", 1)[-1].lower().split("?")[0]
        if ext == "mp3" or play_uri.endswith("/stream.mp3"):
            protocol = "http-get:*:audio/mpeg:*"
        elif ext in ("flac",):
            protocol = "http-get:*:audio/flac:*"
        elif ext in ("aac", "m4a"):
            protocol = "http-get:*:audio/aac:*"
        else:
            protocol = "http-get:*:*:*"

        didl = self._build_stream_didl(
            play_uri, title, artist, album, image_url, protocol
        )
        _LOGGER.debug("[%s] SetAVTransportURI %s", self._name, play_uri.split("?")[0])
        if await self._soap(
            UPNP_SERVICE_AVTRANSPORT,
            "SetAVTransportURI",
            {
                "InstanceID": 0,
                "CurrentURI": play_uri,
                "CurrentURIMetaData": didl,
            },
            raw_fields=_RAW_SOAP_FIELDS,
        ) is None:
            _LOGGER.error("[%s] SetAVTransportURI rejected for %s", self._name, play_uri)
            await self._async_stop_stream_proxy()
            return False

        if await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "Play", {"InstanceID": 0, "Speed": 1}
        ) is None:
            _LOGGER.error("[%s] Play rejected after SetAVTransportURI", self._name)
            await self._async_stop_stream_proxy()
            return False

        # Optimistic HA entity state until the device finishes reading ID3.
        # Stream start routinely resets the AU7001 UPnP HTTP port; hold
        # availability and avoid Zone Get confirmation polling so the native
        # media_player does not flap unavailable while Music Assistant plays.
        self.state = "playing"
        self.media_title = title
        self.media_artist = artist
        self.media_album = album
        if image_url:
            self.media_image_url = image_url
        self._hold_available()

        for _ in range(6):
            await self._async_refresh_transport()
            if self.state in ("playing", "paused"):
                # Prefer device-parsed ID3, but keep our values if firmware
                # has not published them yet.
                if self._stream_metadata:
                    self.media_title = self.media_title or self._stream_metadata.get(
                        "title"
                    )
                    self.media_artist = self.media_artist or self._stream_metadata.get(
                        "artist"
                    )
                    self.media_album = self.media_album or self._stream_metadata.get(
                        "album"
                    )
                    self.media_image_url = (
                        self.media_image_url
                        or self._stream_metadata.get("image_url")
                    )
                return True
            await asyncio.sleep(1)

        _LOGGER.warning(
            "[%s] Stream started but transport still idle; check AU7001 can reach %s",
            self._name,
            play_uri.split("/")[2] if "://" in play_uri else play_uri,
        )
        # Keep optimistic playing — audio often starts after the UPnP blip.
        self.state = "playing"
        return True
