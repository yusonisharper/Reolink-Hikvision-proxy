"""Motion Boxes: the source camera's own target boxes, in the extension area of each live video frame.

A real Reolink camera sends every frame with boxes, and the client draws them. Reolink Client turns its
option on as soon as a frame carries this data (hasAiData sets supportAiMark), with no capability bit
involved. So a camera gets the extension only once it has really sent targets: one that sends none shows
no option. Nothing is computed here, and a camera without the data simply has no boxes.

Where Hikvision has them: RTSP has video and audio, and its isapi.metadata track gives one sample per
target. What Hik-Connect draws comes with the stream of the web preview: a WebSocket on port 7681, RTP
with private packets (payload type 112). The same packets are stored in recordings.

    RTP header, extension profile 3, then from the start of the extension:
    offset 12  ff ff 23 xx
    offset 22  target count
    offset 23  1 byte, then width and height as u16 with the top bit set (coordinates count in these)
    each target, from a byte boundary: 10 zero bits, 1 flag bit, 9 zero bits, 15 bits of id,
    then x, y, width, height, each Exp-Golomb coded like ue(v) in H.265
    xx 0x25: 6 more bytes after each target. 0x23: none. Other values are not known.

Checked on a recording, whose packets of type 9 hold the same boxes as plain floats: 351 of 351 lists agree.

Reolink frame extension, from an NVR main stream capture, checked against libBCSDKWrapper's parser:

    ff <len16> { 01 0b00 { 01 0800 <u64 micros> }
                 02 <len16> { 01 0400 "lz4 "  02 <len16> <LZ4 frame> } }   padded to 8 bytes

Decompressed: TLVs of <u8 type> <u16 length> <value>. Types 1, 2 and 3 are sections of groups 3, 1 and 2,
each group holding time (1), sequence (5) and grid (3). Group 2 also holds the boxes (4):
x1 y1 x2 y2 score as u16, and in section 2 track u16, 0, class u8; in section 3 track u16, class u8.
Type 4 is a constant, its meaning unknown. Frames without boxes have only an empty 2 in each group.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import re
import struct
import time
from dataclasses import dataclass

log = logging.getLogger("motionbox")

GRID_W, GRID_H = 640, 352     # what the Reolink coordinates count in, whatever the stream's size
MAX_BOXES = 16                # the client takes 64
SCORE = 90                    # a camera leaves out of section 1 what it is unsure of: seen at 33
CLASS = 0                     # the class in the capture that appears in all three sections
TRAILER = bytes.fromhex("fc08000000000000000000")

WS_PORT = 7681
# 2: small stream. Some cameras answer deviceError for it, and give the targets with the main stream.
WS_STREAMS = (2, 1)
PRIVATE_TARGETS = 3           # RTP extension profile of the packets holding targets
EXTRA_BYTES = {0x23: 0, 0x25: 6}   # by the fourth byte of the marker: what follows each target
STALE_SECONDS = 0.6           # targets arrive about 12 times a second while there are any, nothing when none
IDLE_SECONDS = 20.0           # a stream this quiet is dead: the video packets stop too
RETRY_SECONDS = 60.0
LINGER_SECONDS = 10.0


@dataclass(frozen=True)
class Box:
    x1: int
    y1: int
    x2: int
    y2: int
    track: int = 0


def _tlv(tag: int, value: bytes) -> bytes:
    return struct.pack("<BH", tag, len(value)) + value


def _lz4_frame(data: bytes) -> bytes:
    """No compression (nothing here repeats for long, and this runs for every frame): one LZ4 block of
    literals. Flags: version 1, independent blocks, 64 KB; 0x82 is the header checksum of these two."""
    n = len(data)
    if n < 15:
        token = bytes([n << 4])
    else:
        rest = n - 15
        token = b"\xf0" + b"\xff" * (rest // 255) + bytes([rest % 255])
    block = token + data
    return b"\x04\x22\x4d\x18\x60\x40\x82" + struct.pack("<I", len(block)) + block + bytes(4)


def _boxes_payload(boxes: tuple[Box, ...], micros: int, seq: int) -> bytes:
    """The decompressed data of one frame."""
    if not boxes:
        empty = _tlv(2, b"")
        group = _tlv(3, empty) + _tlv(1, empty) + _tlv(2, empty)
        return _tlv(1, group) + _tlv(2, group) + _tlv(3, group) + _tlv(4, TRAILER)
    meta = _tlv(1, struct.pack("<Q", micros)) + _tlv(5, struct.pack("<Q", seq)) \
        + _tlv(3, struct.pack("<HH", GRID_W, GRID_H))
    plain = detailed = person = b""
    for b in boxes:
        corners = struct.pack("<5H", b.x1, b.y1, b.x2, b.y2, SCORE)
        plain += _tlv(4, corners)
        detailed += _tlv(4, corners + struct.pack("<HBB", b.track, 0, CLASS))
        person += _tlv(4, corners + struct.pack("<HB", b.track, CLASS))

    def section(items: bytes) -> bytes:
        return _tlv(3, meta) + _tlv(1, meta) + _tlv(2, meta + items)
    return _tlv(1, section(plain)) + _tlv(2, section(detailed)) + _tlv(3, section(person)) + _tlv(4, TRAILER)


class FrameEncoder:
    """One per stream sent: the time inside is the previous frame's, as the result of a frame analyzed
    earlier, and the sequence counts up."""

    def __init__(self) -> None:
        self.seq = 0
        self.previous = 0

    def encode(self, micros: int, boxes: tuple[Box, ...]) -> bytes:
        self.seq += 1
        frame = _lz4_frame(_boxes_payload(boxes, self.previous, self.seq))
        self.previous = micros
        inner = _tlv(1, b"lz4 ") + _tlv(2, frame)
        return _tlv(0xFF, _tlv(1, _tlv(1, struct.pack("<Q", micros))) + _tlv(2, inner))


# ---------------------------------------------------------------------------
# Hikvision's private packets
# ---------------------------------------------------------------------------

class _Bits:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def read(self, count: int) -> int:
        value = 0
        for _ in range(count):
            value = value << 1 | (self.data[self.pos >> 3] >> (7 - (self.pos & 7))) & 1
            self.pos += 1
        return value

    def exp_golomb(self) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 24:
                raise ValueError("code too long")
        return (1 << zeros) - 1 + self.read(zeros)

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


def parse_targets(packet: bytes) -> tuple[Box, ...] | None:
    """A private packet, from its type. None when it is not a target list, or not one understood:
    anything out of place gives no boxes rather than wrong ones."""
    if packet[:2] != struct.pack(">H", PRIVATE_TARGETS) or packet[12:15] != b"\xff\xff\x23" or len(packet) < 28:
        return None
    extra = EXTRA_BYTES.get(packet[15])
    if extra is None:
        return None
    width, height = struct.unpack_from(">HH", packet, 24)
    if not (width & 0x8000 and height & 0x8000):      # the same marker holds other lists, such as the rules
        return None
    width, height = width & 0x7FFF, height & 0x7FFF
    if not (width and height):
        return None
    bits = _Bits(packet[28:])
    boxes = []
    try:
        for _ in range(packet[22]):
            bits.align()
            if bits.read(10):
                return None
            bits.read(1)
            if bits.read(9):
                return None
            track = bits.read(15)
            x, y, w, h = bits.exp_golomb(), bits.exp_golomb(), bits.exp_golomb(), bits.exp_golomb()
            bits.align()
            bits.pos += extra * 8
            if x + w > width or y + h > height or not (w and h):
                return None
            boxes.append(Box(x * GRID_W // width, y * GRID_H // height, (x + w) * GRID_W // width,
                             (y + h) * GRID_H // height, track))
    except IndexError:
        return None
    return tuple(boxes[:MAX_BOXES])


def rtp_private_packet(rtp: bytes) -> bytes | None:
    """What an RTP packet's header extension holds: its profile, length and data. Nothing without one."""
    if len(rtp) < 16 or not rtp[0] & 0x10:
        return None
    return rtp[12 + (rtp[0] & 0x0F) * 4:]


# ---------------------------------------------------------------------------
# WebSocket client, only as far as this stream needs it
# ---------------------------------------------------------------------------

def _ws_frame(payload: bytes, opcode: int) -> bytes:
    mask = os.urandom(4)
    size = len(payload)
    head = bytes([0x80 | opcode]) + (bytes([0x80 | size]) if size < 126 else bytes([0x80 | 126]) + struct.pack(">H", size))
    return head + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload))


async def _ws_read(reader: asyncio.StreamReader) -> tuple[int, bytes]:
    first, second = await reader.readexactly(2)
    size = second & 0x7F
    if size == 126:
        size = struct.unpack(">H", await reader.readexactly(2))[0]
    elif size == 127:
        size = struct.unpack(">Q", await reader.readexactly(8))[0]
    return first & 0x0F, await reader.readexactly(size)


class CameraRefused(Exception):
    """This stream of this camera is not available: another one may be."""


class CameraTargets:
    """Reads the camera's target boxes while somebody watches. supported turns true at the first target list:
    until then frames go out as they always did, so a camera without targets shows no option."""

    def __init__(self, name: str, http, host: str, port: int = WS_PORT) -> None:
        self.name, self.http, self.host, self.port = name, http, host, port
        self.viewers = 0
        self.supported = False
        self._task: asyncio.Task | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self._boxes: tuple[Box, ...] = ()
        self._fresh = float("-inf")
        self._stream = WS_STREAMS[0]

    def boxes(self) -> tuple[Box, ...]:
        return self._boxes if time.monotonic() - self._fresh <= STALE_SECONDS else ()

    def add_viewer(self) -> None:
        self.viewers += 1
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"targets-{self.name}")

    def remove_viewer(self) -> None:
        self.viewers -= 1
        if self.viewers <= 0 and self._task and not self._stop_handle:
            self._stop_handle = asyncio.get_running_loop().call_later(LINGER_SECONDS, self.stop)

    def stop(self) -> None:
        self._stop_handle = None
        if self._task:
            self._task.cancel()
            self._task = None

    def _publish(self, boxes: tuple[Box, ...]) -> None:
        if not self.supported:
            log.info("%s: the camera sends target boxes", self.name)
            self.supported = True
        self._boxes = boxes
        self._fresh = time.monotonic()

    async def _run(self) -> None:
        streams = list(dict.fromkeys((self._stream, *WS_STREAMS)))
        i = 0
        while True:
            self._stream = streams[i % len(streams)]
            try:
                await self._read_stream(self._stream)
                delay = 2.0                         # it ran, and ended: reconnect
            except CameraRefused as e:
                log.debug("%s: stream %d: %s", self.name, self._stream, e)
                i += 1
                delay = RETRY_SECONDS if i % len(streams) == 0 else 0.0
            except (OSError, asyncio.TimeoutError, asyncio.IncompleteReadError, ValueError) as e:
                log.debug("%s: target stream: %s", self.name, e or type(e).__name__)
                delay = RETRY_SECONDS
            if delay:
                await asyncio.sleep(delay)

    async def _token(self) -> str:
        r = await self.http.request("GET", "/ISAPI/Security/token?format=json")
        try:
            body = await asyncio.wait_for(r.read_all(), 6)
        finally:
            r.close()
        found = re.search(rb'"value"\s*:\s*"([^"]+)"', body) if r.status == 200 else None
        if not found:
            raise CameraRefused(f"no session token: HTTP {r.status}")
        return found.group(1).decode()

    async def _read_stream(self, stream: int) -> None:
        token = await self._token()
        reader, writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), 6)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            writer.write((f"GET /1/{stream}?version=0.1&cipherSuites=0&token={token} HTTP/1.1\r\n"
                          f"Host: {self.host}:{self.port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 6)
            if b" 101 " not in head.split(b"\r\n", 1)[0]:
                raise CameraRefused(head.split(b"\r\n", 1)[0].decode("latin1"))
            await asyncio.wait_for(_ws_read(reader), 6)      # its version and cipher
            writer.write(_ws_frame(json.dumps(
                {"sequence": 0, "cmd": "realplay", "url": f"live://{self.host}:{self.port}/1/{stream}",
                 "key": "", "authorization": "", "token": token}).encode(), 1))
            await writer.drain()
            while True:
                opcode, data = await asyncio.wait_for(_ws_read(reader), IDLE_SECONDS)
                if opcode == 2:
                    packet = rtp_private_packet(data)
                    if packet is not None:
                        boxes = parse_targets(packet)
                        if boxes is not None:
                            self._publish(boxes)
                elif opcode == 1:
                    answer = json.loads(data)
                    if answer.get("statusCode", 1) != 1:
                        raise CameraRefused(str(answer.get("subStatusCode")))
                elif opcode == 8:
                    return
                elif opcode == 9:
                    writer.write(_ws_frame(data, 10))
                    await writer.drain()
        finally:
            writer.close()
