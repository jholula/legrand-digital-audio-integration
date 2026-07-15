#!/usr/bin/env python3
"""Poll AU7001 + AU7000 bind health during a capture session.

Discovers the AU7001 via SSDP (survives DHCP IP changes). Match by UDN.

Usage:
  python3 scripts/watch_bind.py \\
    --udn 00000000-0000-0000-0000-0025ed1ccbd1 \\
    --au7000 192.168.68.79 \\
    --log ~/Desktop/legrand-bind-watch.log

Pair with (MAC filter survives IP changes):
  sudo tcpdump -i en0 -s 0 -U -Z "$(whoami)" \\
    -w ~/Desktop/legrand-bind.pcap \\
    '(ether host 00:25:ed:1c:cb:d1) or host 192.168.68.79'
"""

from __future__ import annotations

import argparse
import json
import re
import socket
import sys
import time
import urllib.request
from datetime import datetime
from urllib.parse import urljoin
from xml.etree import ElementTree

ZONE_ST = "urn:schemas-nuvotechnologies-com:device:Zone:1"
ZONE_SVC = "urn:schemas-nuvotechnologies-com:service:Zone:1"
DEFAULT_UDN = "00000000-0000-0000-0000-0025ed1ccbd1"
DEFAULT_AU7000 = "192.168.68.79"
DEFAULT_PORT = 2112


def _ts() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _normalize_udn(value: str) -> str:
    value = value.strip().lower()
    if value.startswith("uuid:"):
        value = value[5:]
    return value.replace("-", "")


def discover_au7001(
    udn: str | None = None,
    host_hint: str | None = None,
    timeout: float = 5.0,
) -> tuple[str, str]:
    """Return (location URL, responder IP) for the AU7001 via SSDP."""
    want = _normalize_udn(udn) if udn else None
    msg = (
        "M-SEARCH * HTTP/1.1\r\n"
        "HOST: 239.255.255.250:1900\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 2\r\n"
        f"ST: {ZONE_ST}\r\n"
        "\r\n"
    ).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.settimeout(0.8)
    deadline = time.time() + timeout
    last_send = 0.0
    try:
        while time.time() < deadline:
            if time.time() - last_send > 1.5:
                sock.sendto(msg, ("239.255.255.250", 1900))
                if host_hint:
                    try:
                        sock.sendto(msg, (host_hint, 1900))
                    except OSError:
                        pass
                last_send = time.time()
            try:
                data, addr = sock.recvfrom(65535)
            except socket.timeout:
                continue
            text = data.decode(errors="replace")
            location = None
            for line in text.split("\r\n"):
                if line.lower().startswith("location:"):
                    location = line.split(":", 1)[1].strip()
            if not location:
                continue
            if want and want not in location.replace("-", "").lower():
                continue
            return location, addr[0]
    finally:
        sock.close()
    raise RuntimeError(
        "SSDP discovery failed for AU7001"
        + (f" udn={udn}" if udn else "")
        + (f" hint={host_hint}" if host_hint else "")
    )


def parse_zone_control_url(location: str) -> str:
    with urllib.request.urlopen(location, timeout=8) as resp:
        body = resp.read().decode(errors="replace")
    for match in re.finditer(
        r"<serviceType>(.*?)</serviceType>\s*<serviceId>.*?</serviceId>\s*"
        r"<SCPDURL>.*?</SCPDURL>\s*<controlURL>(.*?)</controlURL>",
        body,
        re.S,
    ):
        if "service:Zone:1" in match.group(1):
            return urljoin(location, match.group(2).strip())
    raise RuntimeError(f"Zone controlURL not found in {location}")


def zone_get(control_url: str) -> dict[str, str]:
    envelope = f"""<?xml version="1.0"?>
<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
 s:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
  <s:Body>
    <u:Get xmlns:u="{ZONE_SVC}"></u:Get>
  </s:Body>
</s:Envelope>"""
    req = urllib.request.Request(
        control_url,
        data=envelope.encode(),
        method="POST",
        headers={
            "Content-Type": 'text/xml; charset="utf-8"',
            "SOAPAction": f'"{ZONE_SVC}#Get"',
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        xml = resp.read().decode(errors="replace")
    root = ElementTree.fromstring(xml)
    out: dict[str, str] = {}
    for elem in root.iter():
        name = elem.tag.rsplit("}", 1)[-1]
        if name.endswith("Response") or name in {"Envelope", "Body", "Get"}:
            continue
        if elem.text and elem.text.strip():
            out[name] = elem.text.strip()
    return out


def list_sources(host: str, port: int = DEFAULT_PORT) -> list[dict]:
    with socket.create_connection((host, port), timeout=5) as sock:
        sock.settimeout(3)
        try:
            sock.recv(256)
        except OSError:
            pass
        sock.sendall(
            (json.dumps({"ID": 1, "Service": "ListSources"}) + "\n").encode()
        )
        data = b""
        while True:
            chunk = sock.recv(8192)
            if not chunk:
                break
            data += chunk
            if b"\x00" in chunk or b"\n" in data:
                sock.settimeout(0.2)
                try:
                    data += sock.recv(4096)
                except OSError:
                    pass
                break
    text = data.decode(errors="replace").replace("\x00", "").strip()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        payload = json.loads(line)
        return payload.get("SourceList") or []
    raise RuntimeError(f"Unexpected ListSources response: {text[:200]!r}")


def find_dim(sources: list[dict], udn_hint: str | None) -> dict | None:
    want = _normalize_udn(udn_hint) if udn_hint else None
    for src in sources:
        if src.get("Type") != "DIM1":
            continue
        upnp_id = _normalize_udn(src.get("UPnP ID") or "")
        if want and want not in upnp_id and upnp_id not in want:
            continue
        return src
    for src in sources:
        if src.get("Type") == "DIM1":
            return src
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--au7001",
        default="",
        help="Optional last-known IP hint (SSDP still used; IP may change)",
    )
    parser.add_argument(
        "--udn",
        default=DEFAULT_UDN,
        help="AU7001 UUID to match across IP changes",
    )
    parser.add_argument("--au7000", default=DEFAULT_AU7000)
    parser.add_argument("--au7000-port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument(
        "--log",
        default="",
        help="Append timestamped lines to this file (also prints to stdout)",
    )
    args = parser.parse_args()

    log_fh = open(args.log, "a", encoding="utf-8") if args.log else None

    def emit(line: str) -> None:
        print(line, flush=True)
        if log_fh:
            log_fh.write(line + "\n")
            log_fh.flush()

    emit(
        f"{_ts()} START udn={args.udn} au7001_hint={args.au7001 or '(ssdp)'} "
        f"au7000={args.au7000}:{args.au7000_port}"
    )

    location = None
    control_url = None
    au7001_host = args.au7001 or None

    try:
        while True:
            row: dict[str, object] = {"t": _ts()}
            try:
                if location is None:
                    location, au7001_host = discover_au7001(
                        udn=args.udn, host_hint=au7001_host
                    )
                    control_url = parse_zone_control_url(location)
                    emit(
                        f"{_ts()} DISCOVERED host={au7001_host} "
                        f"location={location} control={control_url}"
                    )

                assert control_url is not None
                zone = zone_get(control_url)
                row.update(
                    {
                        "host": au7001_host,
                        "active": zone.get("Active"),
                        "connecting": zone.get("Connecting"),
                        "system_id": zone.get("SystemID"),
                        "member_id": zone.get("MemberID"),
                        "power": zone.get("PowerState"),
                        "title": zone.get("Title"),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                location = None
                control_url = None
                row["au7001_error"] = str(exc)
                row["host"] = au7001_host

            try:
                sources = list_sources(args.au7000, args.au7000_port)
                dim = find_dim(sources, args.udn)
                if dim:
                    row["dim_sid"] = dim.get("SID")
                    row["dim_name"] = dim.get("Name")
                    row["dim_upnp"] = dim.get("UPnP ID")
                    row["dim_connecting"] = dim.get("Connecting")
                    row["dim_play"] = dim.get("playState")
                    row["dim_ok"] = True
                else:
                    row["dim_ok"] = False
                    row["dim_sources"] = [
                        f"{s.get('SID')}:{s.get('Name')}:{s.get('Type')}"
                        for s in sources
                    ]
            except Exception as exc:  # noqa: BLE001
                row["au7000_error"] = str(exc)

            active = str(row.get("active"))
            connecting = str(row.get("connecting"))
            dim_ok = row.get("dim_ok")
            if active == "1" and dim_ok:
                state = "BOUND"
            elif connecting == "1":
                state = "CONNECTING"
            elif active == "0":
                state = "UNBOUND"
            else:
                state = "UNKNOWN"
            emit(f"{_ts()} [{state}] {json.dumps(row, sort_keys=True)}")
            time.sleep(args.interval)
    except KeyboardInterrupt:
        emit(f"{_ts()} STOP")
        return 0
    finally:
        if log_fh:
            log_fh.close()


if __name__ == "__main__":
    sys.exit(main())
