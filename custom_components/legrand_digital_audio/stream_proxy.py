"""HTTP proxy that prepends ID3v2 tags to audio streams.

The AU7001 ignores DIDL-Lite metadata on SetAVTransportURI for HTTP streams
and instead reads title/artist/album from ID3 tags in the audio bytes. Music
Assistant (and similar) flow URLs usually have no tags, so the Legrand app
shows "Unknown artist" / "Unknown album". This proxy inserts an ID3v2 header
before piping the upstream body so the device can display now-playing info.
"""

from __future__ import annotations

import asyncio
import logging
import struct

import aiohttp

_LOGGER = logging.getLogger(__name__)

_PROXY_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=60)


def _syncsafe(size: int) -> bytes:
    return bytes(
        [
            (size >> 21) & 0x7F,
            (size >> 14) & 0x7F,
            (size >> 7) & 0x7F,
            size & 0x7F,
        ]
    )


def _id3_frame(frame_id: bytes, payload: bytes) -> bytes:
    return frame_id + struct.pack(">I", len(payload)) + b"\x00\x00" + payload


def _id3_text_frame(frame_id: bytes, text: str) -> bytes:
    # Encoding 3 = UTF-8
    return _id3_frame(frame_id, b"\x03" + text.encode("utf-8") + b"\x00")


def build_id3v2(
    title: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    image: bytes | None = None,
    image_mime: str = "image/jpeg",
) -> bytes:
    """Build an ID3v2.3 tag the AU7001 can parse from an HTTP audio stream."""
    frames = b""
    if title:
        frames += _id3_text_frame(b"TIT2", title)
    if artist:
        frames += _id3_text_frame(b"TPE1", artist)
    if album:
        frames += _id3_text_frame(b"TALB", album)
    if image:
        # APIC: encoding, mime, picture type (3=cover), description, data
        mime = image_mime.encode("ascii", "ignore") or b"image/jpeg"
        apic = b"\x03" + mime + b"\x00\x03\x00" + image
        frames += _id3_frame(b"APIC", apic)
    if not frames:
        return b""
    return b"ID3" + bytes([3, 0, 0]) + _syncsafe(len(frames)) + frames


class Id3StreamProxy:
    """Serve upstream audio with an ID3v2 header prepended."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        upstream_url: str,
        id3: bytes,
        advertise_host: str,
    ):
        self._session = session
        self._upstream_url = upstream_url
        self._id3 = id3
        self._advertise_host = advertise_host
        self._server: asyncio.AbstractServer | None = None
        self.public_url: str | None = None

    async def start(self) -> str:
        self._server = await asyncio.start_server(self._handle_client, "0.0.0.0", 0)
        sockets = self._server.sockets or []
        if not sockets:
            raise RuntimeError("ID3 stream proxy failed to bind")
        port = sockets[0].getsockname()[1]
        self.public_url = f"http://{self._advertise_host}:{port}/stream.mp3"
        _LOGGER.debug(
            "ID3 stream proxy listening at %s -> %s",
            self.public_url,
            self._upstream_url.split("?")[0][:120],
        )
        return self.public_url

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
            _LOGGER.debug("ID3 stream proxy stopped")

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError):
            writer.close()
            return

        first_line = request.split(b"\r\n", 1)[0].decode("latin-1", "ignore")
        method = first_line.split(" ", 1)[0].upper()
        if method not in ("GET", "HEAD"):
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
            await writer.drain()
            writer.close()
            return

        headers = (
            b"HTTP/1.1 200 OK\r\n"
            b"Content-Type: audio/mpeg\r\n"
            b"Accept-Ranges: none\r\n"
            b"Connection: close\r\n"
            b"Cache-Control: no-cache\r\n"
            b"\r\n"
        )
        try:
            writer.write(headers)
            if method == "HEAD":
                await writer.drain()
                return

            if self._id3:
                writer.write(self._id3)
                await writer.drain()

            async with self._session.get(
                self._upstream_url, timeout=_PROXY_TIMEOUT
            ) as resp:
                resp.raise_for_status()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    writer.write(chunk)
                    await writer.drain()
        except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionResetError, BrokenPipeError) as err:
            _LOGGER.debug("ID3 stream proxy client/upstream ended: %s", err)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass


async def async_local_ip_toward(host: str, port: int) -> str | None:
    """Return the local IP address used to reach host:port."""
    try:
        _reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=5
        )
    except (OSError, asyncio.TimeoutError):
        return None
    try:
        sockname = writer.get_extra_info("sockname")
        if sockname:
            return sockname[0]
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except Exception:
            pass
    return None


async def async_fetch_image(
    session: aiohttp.ClientSession, url: str, max_bytes: int = 256_000
) -> tuple[bytes, str] | None:
    """Fetch album art for optional ID3 APIC embedding."""
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as resp:
            if resp.status >= 400:
                return None
            mime = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            data = await resp.content.read(max_bytes + 1)
            if not data or len(data) > max_bytes:
                return None
            if mime not in ("image/jpeg", "image/png", "image/jpg"):
                mime = "image/jpeg"
            return data, mime
    except (aiohttp.ClientError, asyncio.TimeoutError):
        return None
