"""Shared TCP connection manager for the Legrand Digital Audio device.

Owns the single socket shared by every zone entity, performs the JSON
greeting handshake on each (re)connect, serializes all I/O through one lock,
and transparently reconnects with exponential backoff when the link drops.
"""

from __future__ import annotations

import asyncio
import json
import logging
import socket
import time

_LOGGER = logging.getLogger(__name__)

# Seconds to wait for the TCP connect and for each command response.
CONNECT_TIMEOUT = 10
RESPONSE_TIMEOUT = 10

# Reconnect backoff grows 1s -> 2s -> ... capped here.
MAX_BACKOFF = 60

RECV_BUFFER = 1024


class LegrandConnection:
    """Manages the long-lived control socket to a Legrand Digital Audio unit."""

    def __init__(self, host, port):
        self._host = host
        self._port = port
        self._sock: socket.socket | None = None
        self._lock = asyncio.Lock()
        self._connected = False
        self._buffer = ""
        self._backoff = 1
        self._retry_at = 0.0  # monotonic time before which we won't retry

    @property
    def host(self):
        return self._host

    @property
    def port(self):
        return self._port

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
            self._connected = False
            self._close_socket()
            self._buffer = ""
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

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    async def _connect(self):
        """Open the socket and complete the greeting handshake.

        The caller must hold ``self._lock``. Raises on failure.
        """
        self._close_socket()
        self._buffer = ""

        loop = asyncio.get_running_loop()
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setblocking(False)
        _LOGGER.debug("Connecting to %s:%s", self._host, self._port)
        try:
            await asyncio.wait_for(
                loop.sock_connect(sock, (self._host, self._port)),
                timeout=CONNECT_TIMEOUT,
            )

            # Handshake: the device announces itself with a greeting frame,
            # e.g. {"ID":0,"Service":"Greeting","Status":"Open"}.
            greeting = await asyncio.wait_for(
                loop.sock_recv(sock, RECV_BUFFER), timeout=CONNECT_TIMEOUT
            )
        except Exception:
            sock.close()
            raise

        text = greeting.decode("utf-8").replace("\x00", "").strip()
        _LOGGER.debug("[%s] Greeting: %s", self._host, text)
        if "Greeting" not in text:
            sock.close()
            raise ConnectionError(
                f"Unexpected greeting from {self._host}: {text!r}"
            )

        self._sock = sock
        self._connected = True
        self._backoff = 1
        self._retry_at = 0.0
        _LOGGER.info("Connected to %s:%s", self._host, self._port)

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
