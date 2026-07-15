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
from dataclasses import dataclass, field
from xml.etree import ElementTree
from urllib.parse import urlsplit

import aiohttp
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    NUVO_BROWSE_ROOT,
    NUVO_SERVICE_ZONE,
    UPNP_MAX_VOLUME,
    UPNP_SERVICE_AVTRANSPORT,
    UPNP_SERVICE_CONTENT_DIRECTORY,
    UPNP_SERVICE_RENDERING,
)

_LOGGER = logging.getLogger(__name__)

REQUEST_TIMEOUT = 10
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

# SOAP metadata fields carry embedded DIDL-Lite XML and must not be escaped.
_RAW_SOAP_FIELDS = frozenset(
    {
        "CurrentURIMetaData",
        "TrackURIMetaData",
        "MetaData",
        "ParentMetaData",
    }
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


def _parse_group_id(master_group: str | None) -> str:
    """Extract the group id string from the Zone MasterGroup JSON field."""
    if not master_group:
        return ""
    try:
        data = json.loads(master_group)
        return str(data.get("id", ""))
    except (json.JSONDecodeError, TypeError):
        return ""


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

        # Polled state.
        self.state = "idle"  # idle | playing | paused
        self.active = False
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

    @property
    def available(self) -> bool:
        return self._available

    @property
    def is_active(self) -> bool:
        """True when the AU7001 is fully bound (Zone Active=1)."""
        return self.active

    @property
    def udn(self) -> str:
        return self._udn

    @property
    def name(self) -> str:
        return self._name

    def update_location(self, location: str) -> None:
        """Update the SSDP location (e.g. after the device rebooted on a new port)."""
        if location and location != self._location:
            _LOGGER.debug("[%s] Location updated: %s", self._name, location)
            self._location = location
            self._base = None  # force re-resolve of control URLs

    async def async_close(self):
        """Release browse state (no persistent socket for UPnP)."""
        self._queue_id = None
        self._browse_context = None
        self._items_by_id.clear()

    # ------------------------------------------------------------------
    # Description / control-URL resolution
    # ------------------------------------------------------------------
    async def _async_resolve(self) -> bool:
        """Fetch the device description and resolve control URLs."""
        split = urlsplit(self._location)
        self._base = f"{split.scheme}://{split.netloc}"
        session = async_get_clientsession(self._hass)
        try:
            async with session.get(self._location, timeout=REQUEST_TIMEOUT) as resp:
                resp.raise_for_status()
                body = await resp.text()
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.debug("[%s] Description fetch failed: %s", self._name, e)
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
    ):
        """Invoke a SOAP action; return a dict of response fields or None."""
        if self._base is None and not await self._async_resolve():
            self._available = False
            return None

        raw = raw_fields or _RAW_SOAP_FIELDS
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
        session = async_get_clientsession(self._hass)
        try:
            async with session.post(
                self._control_url(service_type),
                data=envelope.encode("utf-8"),
                headers=headers,
                timeout=REQUEST_TIMEOUT,
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
            self._available = False
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
    async def _async_ensure_queue(self) -> str | None:
        """Create or reuse the ContentDirectory subscription queue."""
        if not self.active:
            _LOGGER.warning(
                "[%s] AU7001 is inactive; complete bind (solid white LED) first",
                self._name,
            )
            return None
        if self._queue_id:
            return self._queue_id

        resp = await self._soap(
            UPNP_SERVICE_CONTENT_DIRECTORY, "X_NUVO_CreateSubscribeQueue", {}
        )
        if not resp or not resp.get("QueueID"):
            return None
        self._queue_id = resp["QueueID"]
        return self._queue_id

    async def async_browse(
        self,
        object_id: str = NUVO_BROWSE_ROOT,
        browse_flag: str = "BrowseDirectChildren",
    ) -> NuvoBrowseResult | None:
        """Browse the on-device music menu via X_NUVO_Browse2."""
        queue_id = await self._async_ensure_queue()
        if not queue_id:
            return None

        resp = await self._soap(
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
        if not resp:
            return None

        result = self._parse_browse_result(object_id, resp)
        if result:
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
            is_playable = tag == "item" and (
                "audioItem" in item_class or "audioBroadcast" in item_class
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
            _LOGGER.warning(
                "[%s] Unknown browse item %s; browse the parent container first",
                self._name,
                item_id,
            )
            return False

        track_uri = item_id if item_id.startswith("nuvo:") else f"nuvo:{item_id}"
        await self._soap(
            UPNP_SERVICE_AVTRANSPORT,
            "X_NUVO_PlayContainerURI",
            {
                "InstanceID": 0,
                "CurrentURI": "",
                "CurrentURIMetaData": context.container_didl,
                "TrackURI": track_uri,
                "TrackURIMetaData": item.didl_xml,
                "StartingIndex": item.index + 1,
                "UpdateID": -1,
            },
            raw_fields=_RAW_SOAP_FIELDS,
        )
        for _ in range(4):
            await self.async_update()
            if self.state == "playing":
                return True
            await asyncio.sleep(1)
        return False

    # ------------------------------------------------------------------
    # State polling
    # ------------------------------------------------------------------
    async def async_update(self):
        """Refresh all polled state from the device."""
        zone = await self._soap(NUVO_SERVICE_ZONE, "Get")
        if zone is None:
            return

        self._available = True
        self.power_state = zone.get("PowerState")
        self.model = zone.get("Model")
        self.active = zone.get("Active") == "1"
        self.group_id = _parse_group_id(zone.get("MasterGroup"))
        if not self.active:
            self._queue_id = None

        transport = await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "GetTransportInfo", {"InstanceID": 0}
        )
        tstate = (transport or {}).get("CurrentTransportState", "")
        if tstate == "PLAYING":
            self.state = "playing"
        elif tstate == "PAUSED_PLAYBACK":
            self.state = "paused"
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

        position = await self._soap(
            UPNP_SERVICE_AVTRANSPORT, "GetPositionInfo", {"InstanceID": 0}
        )
        if position:
            self.media_duration = _parse_duration(position.get("TrackDuration"))
            self.media_position = _parse_duration(position.get("RelTime"))
            self._parse_metadata(position.get("TrackMetaData", ""))

    def _parse_metadata(self, didl: str):
        """Extract title/artist/album/art from DIDL-Lite metadata."""
        self.media_title = None
        self.media_artist = None
        self.media_album = None
        self.media_image_url = None
        if not didl or not didl.strip():
            return
        root = _parse_didl(didl)
        if root is None:
            return
        for elem in root.iter():
            tag = _local(elem.tag)
            text = (elem.text or "").strip()
            if not text:
                continue
            if tag == "title":
                self.media_title = text
            elif tag in ("artist", "creator"):
                self.media_artist = self.media_artist or text
            elif tag == "album":
                self.media_album = text
            elif tag in ("albumArtURI", "icon") and text.startswith("http"):
                self.media_image_url = text

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

    async def async_play_uri(self, uri: str, title: str = "Stream") -> bool:
        """Stream an HTTP/HTTPS URL via standard DLNA SetAVTransportURI.

        Requires the AU7001 to be fully bound (Zone Active=1). Used by Music
        Assistant and other callers that push a reachable audio stream URL.
        """
        if not self.active:
            _LOGGER.warning(
                "[%s] Cannot stream URL while AU7001 is inactive", self._name
            )
            return False

        didl = (
            '<?xml version="1.0"?>'
            '<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/">'
            '<item id="0" parentID="-1" restricted="1">'
            f"<dc:title>{html.escape(title)}</dc:title>"
            "<upnp:class>object.item.audioItem.musicTrack</upnp:class>"
            f'<res protocolInfo="http-get:*:*:*">{uri}</res>'
            "</item></DIDL-Lite>"
        )
        if await self._soap(
            UPNP_SERVICE_AVTRANSPORT,
            "SetAVTransportURI",
            {
                "InstanceID": 0,
                "CurrentURI": uri,
                "CurrentURIMetaData": didl,
            },
            raw_fields=_RAW_SOAP_FIELDS,
        ) is None:
            return False

        await self.async_play()
        for _ in range(4):
            await self.async_update()
            if self.state == "playing":
                return True
            await asyncio.sleep(1)
        return False
