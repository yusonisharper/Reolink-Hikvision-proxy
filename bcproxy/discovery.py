"""Reolink LAN search (UDP 2000).

The Reolink Client / NVR "search" broadcasts the 4 bytes ``aa aa 00 00`` to
UDP 2000 (255.255.255.255 and the subnet broadcast); a Reolink NVR does so
every 2 s from port 3000. Every Reolink device answers with a fixed 388-byte
record, unicast to the sender's IP but always to UDP 3000, whatever the
request's source port.

Reply layout, from real RLC cameras and an NVR on the same LAN
(all strings NUL-padded, numbers little-endian):

  off  size  field
   50   1    01 = UID present (IPC with P2P on), 00 = no UID; NVR sends 02
   51   1    device type: 02 = IPC, 03 = NVR
   52   1    channels? (IPC 01, NVR 0x18)
   54   1    IPC 01, NVR 0x10 (meaning unknown)
   57   1    IPC 01, NVR 00 (meaning unknown)
   80   6    MAC, binary
  104   4    aa aa 00 00 (echo of the command)
  108  20    IP address, ASCII
  128   4    Baichuan TCP port (9000)
  132  32    device name, UTF-8
  164  64    MAC, "EC:71:DB:12:34:56"
  228  64    UID
  292  96    serial (NVR only)
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import socket
import struct

log = logging.getLogger("discovery")

PORT = 2000
REPLY_PORT = 3000
REQUEST = b"\xaa\xaa\x00\x00"
REPLY_LEN = 388
REOLINK_OUI = "EC:71:DB"


def is_request(data: bytes) -> bool:
    return data[:4] == REQUEST


def _cstr(s: str, size: int) -> bytes:
    """UTF-8, cut on a character boundary so that a terminating NUL fits."""
    b = s.encode("utf-8")[:size - 1]
    return b.decode("utf-8", "ignore").encode("utf-8").ljust(size, b"\x00")


def _str(b: bytes) -> str:
    return b.split(b"\x00", 1)[0].decode("utf-8", "replace")


def default_mac(seed: str) -> str:
    """Stable per-camera MAC in Reolink's OUI (NVRs list devices by MAC)."""
    h = hashlib.md5(seed.encode("utf-8")).digest()
    return REOLINK_OUI + "".join(f":{x:02X}" for x in h[:3])


def build_reply(ip: str, port: int, name: str, mac: str, uid: str = "") -> bytes:
    mac_bin = bytes.fromhex(mac.replace(":", "").replace("-", ""))
    if len(mac_bin) != 6:
        raise ValueError(f"bad MAC {mac!r}")
    b = bytearray(REPLY_LEN)
    b[50:58] = bytes([1 if uid else 0, 0x02, 0x01, 0x00, 0x01, 0x00, 0x00, 0x01])
    b[80:86] = mac_bin
    b[104:108] = REQUEST
    b[108:128] = _cstr(ip, 20)
    b[128:132] = struct.pack("<I", port)
    b[132:164] = _cstr(name, 32)
    b[164:228] = _cstr(":".join(f"{x:02X}" for x in mac_bin), 64)
    b[228:292] = _cstr(uid, 64)
    return bytes(b)


def parse_reply(data: bytes) -> dict | None:
    if len(data) < 292 or data[104:108] != REQUEST:
        return None
    return {
        "type": {2: "IPC", 3: "NVR"}.get(data[51], f"0x{data[51]:02x}"),
        "ip": _str(data[108:128]),
        "port": struct.unpack_from("<I", data, 128)[0],
        "name": _str(data[132:164]),
        "mac": _str(data[164:228]).upper(),
        "uid": _str(data[228:292]),
        "serial": _str(data[292:388]),
    }


def local_ip_for(peer: str) -> str:
    """Address of the interface we would use to reach ``peer``."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect((peer, PORT))
            return s.getsockname()[0]
        except OSError:
            return "0.0.0.0"


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, cams: list, reply_port: int) -> None:
        self.cams = cams
        self.reply_port = reply_port
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport) -> None:
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        if not is_request(data):
            log.debug("ignored %d bytes from %s:%d: %s", len(data), addr[0], addr[1], data[:16].hex())
            return
        for cam in self.cams:
            ip = cam.cfg.listen_host
            if ip in ("", "0.0.0.0"):
                ip = local_ip_for(addr[0])
            reply = build_reply(ip, cam.cfg.listen_port, cam.display_name,
                                cam.mac, cam.uid)
            self.transport.sendto(reply, (addr[0], self.reply_port))
        log.debug("search from %s:%d answered for %d camera(s)", addr[0], addr[1], len(self.cams))

    def error_received(self, exc) -> None:  # e.g. ICMP port unreachable on Windows
        log.debug("udp error: %s", exc)


async def serve(cams: list, host: str = "0.0.0.0", port: int = PORT,
                reply_port: int = REPLY_PORT) -> asyncio.DatagramTransport:
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(lambda: _Protocol(cams, reply_port),
                                                       local_addr=(host, port))
    return transport
