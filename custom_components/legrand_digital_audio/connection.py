"""Shared TCP connection manager for the Legrand Digital Audio device.

Owns the single socket shared by every zone entity, performs the greeting
handshake on each (re)connect, serializes all I/O through one lock, and
transparently reconnects with exponential backoff when the link drops.

Older AU7000 firmware sends a null-terminated JSON frame (Greeting, ping, or
MAC). Newer firmware, per Legrand's 2020 security addendum, sends
``Hello V1 \\x00 <32-hex challenge> <12-hex MAC>`` and expects
``hex(AES-128-ECB(MD5(password), challenge))`` with no terminator.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import socket
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the TCP connect and for each command response.
CONNECT_TIMEOUT = 10
RESPONSE_TIMEOUT = 10

# Reconnect backoff grows 1s -> 2s -> ... capped here.
MAX_BACKOFF = 60

RECV_BUFFER = 1024

# "Hello V1 " including the trailing space, then a NUL, then the challenge.
_HELLO_PREFIX = b"Hello V1 "
_CHALLENGE_RE = re.compile(rb"([0-9A-Fa-f]{32}) ([0-9A-Fa-f]{12})")
_AUTH_TOKENS = (b"[OK]", b"[INVALID]", b"[SETKEY]")
_SETKEY_MESSAGE = (
    "This AU7000 has no password set. Set a password in the Legrand app, "
    "then try again."
)


class LegrandAuthRequired(ConnectionError):
    """The AU7000 sent a security challenge and no password was configured."""


class LegrandInvalidAuth(ConnectionError):
    """The AU7000 rejected the password."""


class LegrandSetKeyRequired(ConnectionError):
    """The AU7000 is waiting for a password to be set in the Legrand app."""


def encrypt_challenge(password: str, challenge_hex: str) -> str:
    """Return the Hello V1 response for a factory password and challenge.

    The key is MD5(password) and the 16-byte challenge is encrypted with
    AES-128-ECB. The device expects the ciphertext as 32 lowercase hex
    characters with no terminator. MD5 is required by the Legrand protocol.
    """
    digest = hashes.Hash(hashes.MD5())
    digest.update(password.encode("utf-8"))
    key = digest.finalize()
    challenge = bytes.fromhex(challenge_hex)
    if len(challenge) != 16:
        raise ValueError("security challenge must be 16 bytes")
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return (encryptor.update(challenge) + encryptor.finalize()).hex()


class LegrandConnection:
    """Manages the long-lived control socket to a Legrand Digital Audio unit."""

    def __init__(self, host, port, password: str | None = None):
        self._host = host
        self._port = port
        self._password = password or None
        self._sock: socket.socket | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._buffer = ""
        self._backoff = 1
        self._retry_at = 0.0  # monotonic time before which we won't retry
        self._used_password = False

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        return self._port

    @property
    def password(self) -> str | None:
        """Factory password used for the secure handshake, if any."""
        return self._password

    @property
    def used_password(self) -> bool:
        """Whether the last successful connect completed a Hello V1 handshake."""
        return self._used_password

    @property
    def available(self) -> bool:
        """Whether the socket is currently connected and handshaked."""
        return self._connected

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    async def async_connect(self):
        """Establish the initial connection. Raises on failure."""
        async with self._lock:
            await self._connect()

    async def async_close(self):
        """Close the connection and stop serving commands."""
        async with self._lock:
            was_open = self._connected or self._sock is not None
            self._connected = False
            self._close_socket()
            self._buffer = ""
            if was_open:
                _LOGGER.info("Closed connection to %s:%s", self._host, self._port)

    async def send(self, command: str):
        """Send a JSON command string and return the matching response.

        Returns ``None`` on any failure (timeout, parse error, or a dropped
        link). A dropped link is recorded so the next call reconnects once the
        backoff window elapses.
        """
        async with self._lock:
            if not self._connected:
                await self._maybe_reconnect()
                if not self._connected:
                    return None

            try:
                command_id = json.loads(command).get("ID")
            except (ValueError, TypeError):
                _LOGGER.error("[%s] Invalid command payload: %s", self._host, command)
                return None

            loop = asyncio.get_running_loop()
            try:
                _LOGGER.debug("[%s] Sent: %s", self._host, command)
                await loop.sock_sendall(self._sock, (command + "\n").encode("utf-8"))
                return await asyncio.wait_for(
                    self._read_response(command_id), timeout=RESPONSE_TIMEOUT
                )
            except asyncio.TimeoutError:
                # A missed reply on a healthy link is abnormal; drop the socket
                # so the next cycle re-handshakes rather than wedging forever.
                _LOGGER.warning(
                    "[%s] Timeout waiting for response to command ID %s; "
                    "dropping connection",
                    self._host,
                    command_id,
                )
                self._mark_disconnected()
                return None
            except (OSError, ConnectionError) as e:
                _LOGGER.warning(
                    "[%s] Socket error (%s); marking disconnected", self._host, e
                )
                self._mark_disconnected()
                return None

    async def fetch_zones(self) -> list[dict]:
        """Return zone dicts from ListSources and ListZones.

        The caller must already be connected. Raises ``ConnectionError`` when
        either reply is missing, and ``RuntimeError`` when the unit reports
        no zones.
        """
        sources = await self.send(json.dumps({"ID": 3, "Service": "ListSources"}))
        if not sources or "SourceList" not in sources:
            raise ConnectionError(f"ListSources failed on {self._host}")

        zones_resp = await self.send(json.dumps({"ID": 4, "Service": "ListZones"}))
        if not zones_resp or "ZoneList" not in zones_resp:
            raise ConnectionError(f"ListZones failed on {self._host}")

        devices = []
        for zone in zones_resp["ZoneList"]:
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _connect(self):
        """Open the socket and complete the greeting handshake.

        The caller must hold ``self._lock``. Raises on failure.
        """
        self._close_socket()
        self._buffer = ""
        self._used_password = False

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        _LOGGER.debug("Connecting to %s:%s", self._host, self._port)
        try:
            await asyncio.wait_for(
                loop.sock_connect(sock, (self._host, self._port)),
                timeout=CONNECT_TIMEOUT,
            )
            leftover = await self._handshake(sock)
        except Exception:
            sock.close()
            raise

        self._sock = sock
        self._buffer = leftover
        self._connected = True
        self._backoff = 1
        self._retry_at = 0.0
        _LOGGER.info("Connected to %s:%s", self._host, self._port)

    async def _handshake(self, sock: socket.socket) -> str:
        """Accept a JSON greeting or complete Hello V1 authentication.

        Returns leftover text (possibly more null-terminated frames) to seed
        the read buffer. The caller must hold ``self._lock``.
        """
        deadline = asyncio.get_running_loop().time() + CONNECT_TIMEOUT
        buf = b""
        while True:
            buf = await self._recv_more(sock, buf, deadline)
            buf = buf.lstrip(b"\x00")
            if not buf:
                continue

            if buf.startswith(b"[SETKEY]"):
                raise LegrandSetKeyRequired(_SETKEY_MESSAGE)

            if buf.startswith(_HELLO_PREFIX):
                rest = await self._finish_hello(sock, buf, deadline)
                return rest.decode("utf-8", errors="replace")

            # "Hell" or "[SET" can still grow into a known prefix.
            if _HELLO_PREFIX.startswith(buf) or b"[SETKEY]".startswith(buf):
                continue

            if buf.startswith((b"{", b" ", b"\n", b"\r")):
                accepted, buf = self._accept_json_greeting(buf)
                if accepted is None:
                    continue
                return accepted.decode("utf-8", errors="replace")

            raise ConnectionError(
                f"Unexpected greeting from {self._host}: {buf[:80]!r}"
            )

    def _accept_json_greeting(self, buf: bytes) -> tuple[bytes | None, bytes]:
        """Return leftover bytes after a JSON greeting, and the unparsed tail.

        The leftover is ``None`` when the buffer does not yet hold a complete
        JSON object. An empty null-terminated frame is skipped.
        """
        while True:
            if b"\x00" not in buf:
                text = buf.decode("utf-8", errors="replace").strip()
                if text.endswith("}") and self._looks_like_json_object(text):
                    _LOGGER.debug("[%s] Greeting: %s", self._host, text)
                    return b"", b""
                return None, buf

            frame, _, rest = buf.partition(b"\x00")
            text = frame.decode("utf-8", errors="replace").strip()
            if not text:
                buf = rest.lstrip(b"\x00")
                if buf:
                    continue
                return None, b""

            self._require_json_object(text)
            _LOGGER.debug("[%s] Greeting: %s", self._host, text)
            return rest, rest

    def _require_json_object(self, text: str) -> None:
        if not self._looks_like_json_object(text):
            raise ConnectionError(
                f"Unexpected greeting from {self._host}: {text!r}"
            )

    @staticmethod
    def _looks_like_json_object(text: str) -> bool:
        try:
            message = json.loads(text)
        except json.JSONDecodeError:
            return False
        return isinstance(message, dict)

    async def _finish_hello(
        self, sock: socket.socket, buf: bytes, deadline: float
    ) -> bytes:
        """Answer a Hello V1 challenge and return bytes that follow ``[OK]``."""
        payload = buf[len(_HELLO_PREFIX) :]
        match = None
        while match is None:
            payload = payload.lstrip(b"\x00")
            match = _CHALLENGE_RE.match(payload)
            if match is not None:
                break
            if len(payload) > 64:
                raise ConnectionError(
                    f"Malformed security challenge from {self._host}: "
                    f"{payload[:48]!r}"
                )
            payload = await self._recv_more(sock, payload, deadline)

        challenge = match.group(1).decode("ascii")
        mac = match.group(2).decode("ascii")
        leftover = payload[match.end() :]
        _LOGGER.debug("[%s] Security challenge from %s", self._host, mac)
        if not self._password:
            raise LegrandAuthRequired(
                f"AU7000 at {self._host} requires the password printed on the module"
            )

        token = encrypt_challenge(self._password, challenge)
        await asyncio.get_running_loop().sock_sendall(sock, token.encode("ascii"))
        _LOGGER.debug("[%s] Sent security response", self._host)
        return await self._read_auth_result(sock, leftover, deadline)

    async def _read_auth_result(
        self, sock: socket.socket, buf: bytes, deadline: float
    ) -> bytes:
        """Consume ``[OK]``, ``[INVALID]``, or ``[SETKEY]`` and return leftovers."""
        while True:
            buf = buf.lstrip(b"\x00\r\n ")
            if buf.startswith(b"[OK]"):
                self._used_password = True
                return await self._after_ok(sock, buf[4:], deadline)
            if buf.startswith(b"[INVALID]"):
                raise LegrandInvalidAuth(
                    f"AU7000 at {self._host} rejected the password"
                )
            if buf.startswith(b"[SETKEY]"):
                raise LegrandSetKeyRequired(_SETKEY_MESSAGE)
            if buf and not any(token.startswith(buf) for token in _AUTH_TOKENS):
                raise ConnectionError(
                    f"Unexpected authentication reply from {self._host}: {buf[:40]!r}"
                )
            buf = await self._recv_more(sock, buf, deadline)

    async def _after_ok(
        self, sock: socket.socket, rest: bytes, deadline: float
    ) -> bytes:
        """Drop the ``[OK]`` trailer and return any following frames.

        The addendum shows the success trailer as ``[OK]\\n\\r\\n\\x00``.
        """
        while b"\x00" not in rest:
            stripped = rest.lstrip(b"\r\n ")
            if stripped.startswith(b"{"):
                return stripped
            if len(rest) > 8 and stripped:
                return stripped
            rest = await self._recv_more(sock, rest, deadline)
        _, _, after = rest.partition(b"\x00")
        return after

    async def _recv_more(
        self, sock: socket.socket, buf: bytes, deadline: float
    ) -> bytes:
        loop = asyncio.get_running_loop()
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise TimeoutError(f"Timed out during handshake with {self._host}")
        chunk = await asyncio.wait_for(
            loop.sock_recv(sock, RECV_BUFFER), timeout=remaining
        )
        if not chunk:
            raise ConnectionError(
                f"Connection closed by {self._host} during handshake"
            )
        return buf + chunk

    async def _maybe_reconnect(self):
        """Attempt a single reconnect if the backoff window has elapsed."""
        if time.monotonic() < self._retry_at:
            return
        try:
            await self._connect()
        except Exception as e:  # noqa: BLE001 - any failure just schedules a retry
            delay = self._backoff
            # First failure is worth a warning; keep the retry storm at debug.
            log = _LOGGER.warning if delay <= 1 else _LOGGER.debug
            log(
                "Reconnect to %s:%s failed (%s); next retry in %ss",
                self._host,
                self._port,
                e,
                delay,
            )
            self._schedule_retry()

    async def _read_response(self, sent_command_id):
        """Read null-framed JSON messages until the matching reply arrives."""
        loop = asyncio.get_running_loop()
        while True:
            data = await loop.sock_recv(self._sock, RECV_BUFFER)
            if not data:
                raise ConnectionResetError("connection closed by device")

            self._buffer += data.decode("utf-8")
            messages = self._buffer.split("\x00")
            self._buffer = messages.pop()

            for message in messages:
                message = message.strip()
                if not message:
                    continue
                try:
                    response_json = json.loads(message)
                except json.JSONDecodeError:
                    _LOGGER.error("[%s] Failed to parse JSON: %s", self._host, message)
                    continue

                _LOGGER.debug("[%s] Received: %s", self._host, response_json)
                response_id = response_json.get("ID")
                if response_id == sent_command_id:
                    return response_json
                # Greeting (ID 0) and unsolicited event frames are expected.
                _LOGGER.debug(
                    "[%s] Ignoring unmatched frame (ID %s, awaiting %s)",
                    self._host,
                    response_id,
                    sent_command_id,
                )

    def _mark_disconnected(self):
        """Record a dropped link and schedule the next reconnect attempt."""
        was_connected = self._connected
        self._connected = False
        self._close_socket()
        self._buffer = ""
        self._schedule_retry()
        if was_connected:
            _LOGGER.warning("[%s] Connection lost", self._host)

    def _schedule_retry(self):
        self._retry_at = time.monotonic() + self._backoff
        self._backoff = min(self._backoff * 2, MAX_BACKOFF)

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
