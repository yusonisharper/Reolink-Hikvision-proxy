"""Hikvision ISAPI side: event stream, snapshots, and event normalization.

Event sources supported:

* ``alertstream`` (default): long-lived GET /ISAPI/Event/notification/alertStream,
  a multipart/mixed stream of <EventNotificationAlert> documents.
* ``listen``: the camera pushes alerts to us (Configuration > Network >
  Advanced > HTTP Listening / Alarm Server). Use when the camera limits the
  number of alertStream connections.

Only stdlib + asyncio; HTTP Digest auth is implemented here.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import AsyncIterator, Callable

from .events import DetEvent

log = logging.getLogger("hik")

DEFAULT_EVENT_MAP = {
    "vmd": "motion",
    "motiondetection": "motion",
    "linedetection": "crossline",
    "fielddetection": "intrusion",
    "regionentrance": "intrusion",
    "regionexiting": "intrusion",
    "loitering": "loitering",
    "loiteringdetection": "loitering",
    "tamperdetection": "tamper",
    "shelteralarm": "tamper",
}
DEFAULT_TARGET_MAP = {
    "human": "people",
    "person": "people",
    "people": "people",
    "vehicle": "vehicle",
    "car": "vehicle",
    "motorvehicle": "vehicle",
    "nonmotorvehicle": "vehicle",
    "animal": "dog_cat",
}


# ---------------------------------------------------------------------------
# Minimal async HTTP/1.1 client with Digest/Basic auth
# ---------------------------------------------------------------------------

def _parse_www_auth(value: str) -> tuple[str, dict[str, str]]:
    scheme, _, rest = value.partition(" ")
    params = {k: (a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', rest)}
    return scheme.lower(), params


class DigestAuth:
    def __init__(self, user: str, password: str) -> None:
        self.user, self.password = user, password
        self.chal: dict[str, str] | None = None
        self.scheme = "digest"
        self.nc = 0

    def header(self, method: str, uri: str) -> str | None:
        if self.chal is None:
            return None
        if self.scheme == "basic":
            import base64
            tok = base64.b64encode(f"{self.user}:{self.password}".encode()).decode()
            return f"Basic {tok}"
        c = self.chal
        algo = c.get("algorithm", "MD5").upper()
        H = (lambda s: hashlib.sha256(s.encode()).hexdigest()) if algo.startswith("SHA-256") \
            else (lambda s: hashlib.md5(s.encode()).hexdigest())
        ha1 = H(f"{self.user}:{c['realm']}:{self.password}")
        ha2 = H(f"{method}:{uri}")
        qop = c.get("qop")
        parts = [f'username="{self.user}"', f'realm="{c["realm"]}"', f'nonce="{c["nonce"]}"',
                 f'uri="{uri}"', f"algorithm={algo}"]
        if qop:
            qop = "auth" if "auth" in qop.split(",") else qop.split(",")[0].strip()
            self.nc += 1
            nc = f"{self.nc:08x}"
            cnonce = os.urandom(8).hex()
            resp = H(f"{ha1}:{c['nonce']}:{nc}:{cnonce}:{qop}:{ha2}")
            parts += [f"qop={qop}", f"nc={nc}", f'cnonce="{cnonce}"']
        else:
            resp = H(f"{ha1}:{c['nonce']}:{ha2}")
        parts.append(f'response="{resp}"')
        if "opaque" in c:
            parts.append(f'opaque="{c["opaque"]}"')
        return "Digest " + ", ".join(parts)

    def challenge(self, www: str) -> None:
        scheme, params = _parse_www_auth(www)
        self.scheme = scheme
        self.chal = params
        self.nc = 0


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter

    async def body_iter(self) -> AsyncIterator[bytes]:
        te = self.headers.get("transfer-encoding", "").lower()
        if "chunked" in te:
            while True:
                line = await self.reader.readline()
                if not line:
                    return
                size = int(line.split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    await self.reader.readline()
                    return
                yield await self.reader.readexactly(size)
                await self.reader.readline()
        elif "content-length" in self.headers:
            left = int(self.headers["content-length"])
            while left > 0:
                chunk = await self.reader.read(min(65536, left))
                if not chunk:
                    return
                left -= len(chunk)
                yield chunk
        else:
            while True:
                chunk = await self.reader.read(65536)
                if not chunk:
                    return
                yield chunk

    async def read_all(self) -> bytes:
        return b"".join([c async for c in self.body_iter()])

    def close(self) -> None:
        self.writer.close()


class HttpStatusError(ConnectionError):
    """The camera answered, with an error: unlike a network failure, retrying will not help."""


class HikHttp:
    connection = "keep-alive"

    def __init__(self, host: str, port: int, user: str, password: str, timeout: float = 10) -> None:
        self.host, self.port = host, port
        self.auth = DigestAuth(user, password)
        self.timeout = timeout

    async def _send(self, method: str, path: str, body: bytes | None = None,
                    ctype: str = "application/xml") -> HttpResponse:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), self.timeout)
        return await self.exchange(reader, writer, method, path, body, ctype)

    async def exchange(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, method: str, path: str,
                       body: bytes | None = None, ctype: str = "application/xml",
                       connection: str | None = None) -> HttpResponse:
        """One request on an open connection, up to the end of the response headers."""
        hdrs = [f"{method} {path} HTTP/1.1", f"Host: {self.host}:{self.port}",
                "User-Agent: reolink-hik-proxy", "Accept: */*", f"Connection: {connection or self.connection}"]
        a = self.auth.header(method, path)
        if a:
            hdrs.append(f"Authorization: {a}")
        if body is not None:
            hdrs += [f"Content-Type: {ctype}", f"Content-Length: {len(body)}"]
        writer.write(("\r\n".join(hdrs) + "\r\n\r\n").encode() + (body or b""))
        await writer.drain()
        status_line = await asyncio.wait_for(reader.readline(), self.timeout)
        if not status_line:
            writer.close()
            raise ConnectionError("empty HTTP response")
        status = int(status_line.split()[1])
        headers: dict[str, str] = {}
        while True:
            line = await asyncio.wait_for(reader.readline(), self.timeout)
            if line in (b"\r\n", b"\n", b""):
                break
            k, _, v = line.decode("latin1").partition(":")
            headers[k.strip().lower()] = v.strip()
        return HttpResponse(status, headers, reader, writer)

    async def request(self, method: str, path: str, body: bytes | None = None) -> HttpResponse:
        r = await self._send(method, path, body)
        if r.status == 401 and "www-authenticate" in r.headers:
            try:
                if "content-length" in r.headers:
                    await asyncio.wait_for(r.read_all(), 2)
            except Exception:
                pass
            r.close()
            self.auth.challenge(r.headers["www-authenticate"])
            r = await self._send(method, path, body)
        return r

    async def post_bytes(self, path: str, body: bytes) -> tuple[int, bytes]:
        return await self.put_bytes(path, body, method="POST")

    async def put_bytes(self, path: str, body: bytes, method: str = "PUT") -> tuple[int, bytes]:
        r = await self.request(method, path, body)
        try:
            data = await asyncio.wait_for(r.read_all(), self.timeout) if (
                "content-length" in r.headers or "chunked" in r.headers.get("transfer-encoding", "")) else b""
            return r.status, data
        finally:
            r.close()

    async def get_bytes(self, path: str) -> bytes:
        r = await self.request("GET", path)
        try:
            if r.status != 200:
                raise HttpStatusError(f"GET {path} -> HTTP {r.status}")
            return await asyncio.wait_for(r.read_all(), self.timeout)
        finally:
            r.close()


# ---------------------------------------------------------------------------
# multipart/mixed stream splitting
# ---------------------------------------------------------------------------

class MultipartSplitter:
    def __init__(self, boundary: str) -> None:
        b = boundary.strip('"')
        self.delim = b"--" + b.encode()
        self.buf = b""

    def feed(self, data: bytes) -> list[tuple[dict[str, str], bytes]]:
        self.buf += data
        out = []
        while True:
            i = self.buf.find(self.delim)
            if i < 0:
                if len(self.buf) > 1 << 20:      # no boundary in 1MB: discard
                    self.buf = self.buf[-len(self.delim):]
                return out
            hstart = i + len(self.delim)
            hend = self.buf.find(b"\r\n\r\n", hstart)
            if hend < 0:
                return out
            headers = {}
            for line in self.buf[hstart:hend].split(b"\r\n"):
                k, _, v = line.decode("latin1").partition(":")
                if v:
                    headers[k.strip().lower()] = v.strip()
            bstart = hend + 4
            if "content-length" in headers:
                bend = bstart + int(headers["content-length"])
                if len(self.buf) < bend:
                    return out
                nxt = bend
            else:
                bend = self.buf.find(self.delim, bstart)
                if bend < 0:
                    return out
                nxt = bend
                bend = bend - 2 if self.buf[bend - 2:bend] == b"\r\n" else bend
            out.append((headers, self.buf[bstart:bend]))
            self.buf = self.buf[nxt:]


# ---------------------------------------------------------------------------
# EventNotificationAlert parsing
# ---------------------------------------------------------------------------

@dataclass
class HikAlert:
    event_type: str
    state: str                       # active / inactive
    channel: int | None
    regions: list[tuple[int | None, str | None]] = field(default_factory=list)  # (regionID, target)
    target: str | None = None        # top-level targetType if present
    raw: str = ""


def _strip_ns(root: ET.Element) -> None:
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]


def _int(text: str | None) -> int | None:
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def parse_alert(xml_bytes: bytes) -> HikAlert | None:
    try:
        root = ET.fromstring(xml_bytes.strip())
    except ET.ParseError:
        return None
    _strip_ns(root)
    if root.tag != "EventNotificationAlert":
        return None
    get = lambda tag: (root.findtext(tag) or "").strip() or None  # noqa: E731
    ch = _int(get("channelID")) or _int(get("dynChannelID")) or _int(get("dynVideoInputChannelID")) \
        or _int(get("videoInputChannelID"))
    regions = []
    for entry in root.iter("DetectionRegionEntry"):
        rid = _int((entry.findtext("regionID") or "").strip() or None)
        tgt = (entry.findtext("detectionTarget") or entry.findtext("targetType") or "").strip() or None
        regions.append((rid, tgt))
    target = None
    for tag in ("targetType", "detectionTarget"):
        el = root.find(tag)
        if el is not None and (el.text or "").strip():
            target = el.text.strip()
    return HikAlert(
        event_type=(get("eventType") or "").strip(),
        state=(get("eventState") or "active").lower(),
        channel=ch,
        regions=regions,
        target=target,
        raw=xml_bytes.decode("utf-8", "replace"),
    )


@dataclass
class EventMapper:
    event_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_EVENT_MAP))
    target_map: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TARGET_MAP))
    # Smart events without a target (non-AcuSense cameras) can be reported as
    # a target so they still trigger AI recording on the NVR.
    untargeted_smart_target: str | None = None
    channel: int | None = None       # only accept alerts for this Hik channel (None = any)
    region_base: int = 1             # Hik regionID numbering starts at 1

    def map(self, a: HikAlert) -> list[DetEvent]:
        kind = self.event_map.get(a.event_type.lower())
        if kind is None:
            return []
        if self.channel is not None and a.channel is not None and a.channel != self.channel:
            return []
        active = a.state != "inactive"

        def tmap(t: str | None) -> str | None:
            return self.target_map.get(t.lower()) if t else None

        out: list[DetEvent] = []
        regions = a.regions or [(None, None)]
        for rid, tgt in regions:
            target = tmap(tgt) or tmap(a.target)
            if target is None and kind not in ("motion", "tamper"):
                target = self.untargeted_smart_target
            zone = None if rid is None else max(0, rid - self.region_base)
            out.append(DetEvent(kind, active, zone, frozenset([target]) if target else frozenset()))
        return out


# ---------------------------------------------------------------------------
# Event sources
# ---------------------------------------------------------------------------

async def alert_stream(http: HikHttp, on_alert: Callable[[HikAlert], None],
                       idle_timeout: float = 60.0, path: str = "/ISAPI/Event/notification/alertStream") -> None:
    """Run forever, reconnecting on errors."""
    backoff = 1.0
    while True:
        r = None
        try:
            r = await http.request("GET", path)
            if r.status != 200:
                raise ConnectionError(f"alertStream HTTP {r.status}")
            ctype = r.headers.get("content-type", "")
            m = re.search(r'boundary="?([^";]+)"?', ctype)
            splitter = MultipartSplitter(m.group(1) if m else "boundary")
            log.info("alertStream connected (%s)", ctype)
            backoff = 1.0
            it = r.body_iter().__aiter__()
            while True:
                chunk = await asyncio.wait_for(it.__anext__(), idle_timeout)
                for headers, body in splitter.feed(chunk):
                    if "xml" not in headers.get("content-type", "xml"):
                        continue
                    alert = parse_alert(body)
                    if alert:
                        on_alert(alert)
        except (StopAsyncIteration, asyncio.TimeoutError, OSError, ConnectionError, ValueError) as e:
            log.warning("alertStream: %s; reconnecting in %.0fs", e or type(e).__name__, backoff)
        finally:
            if r:
                r.close()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


async def listen_server(host: str, port: int, on_alert: Callable[[str, HikAlert], None]) -> asyncio.base_events.Server:
    """HTTP server for Hikvision 'HTTP Listening' push mode."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = (writer.get_extra_info("peername") or ("?",))[0]
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                headers = {}
                while True:
                    h = await reader.readline()
                    if h in (b"\r\n", b"\n", b""):
                        break
                    k, _, v = h.decode("latin1").partition(":")
                    headers[k.strip().lower()] = v.strip()
                body = b""
                if "content-length" in headers:
                    body = await reader.readexactly(int(headers["content-length"]))
                ctype = headers.get("content-type", "")
                docs = []
                m = re.search(r'boundary="?([^";]+)"?', ctype)
                if m:
                    docs = [b for h, b in MultipartSplitter(m.group(1)).feed(body + b"\r\n--" + m.group(1).encode())
                            if "xml" in h.get("content-type", "xml") or b"EventNotificationAlert" in b[:200]]
                else:
                    docs = [body]
                for d in docs:
                    a = parse_alert(d)
                    if a:
                        on_alert(peer, a)
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: keep-alive\r\n\r\n")
                await writer.drain()
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()

    return await asyncio.start_server(handle, host, port)
