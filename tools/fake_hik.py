#!/usr/bin/env python3
"""Fake Hikvision ISAPI server for testing (Digest auth, alertStream, snapshot).

Interactive: type commands on stdin to emit alerts, e.g.
    vmd                 motion (active)
    vmd human           motion with AcuSense target
    line 1 vehicle      line crossing, region 1, vehicle
    field 2 human       intrusion (fielddetection), region 2
    loiter 1 human
    vmd off             VMD inactive
Or script with --script "vmd human;sleep 2;line 1 vehicle".
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import os
import re
import time

ALERT = """<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>192.168.1.64</ipAddress>
<portNo>80</portNo>
<protocol>HTTP</protocol>
<macAddress>bc:ad:28:00:00:01</macAddress>
<channelID>{ch}</channelID>
<dateTime>{dt}</dateTime>
<activePostCount>1</activePostCount>
<eventType>{etype}</eventType>
<eventState>{state}</eventState>
<eventDescription>{etype} alarm</eventDescription>
{regions}
</EventNotificationAlert>
"""
REGION = """<DetectionRegionList>
<DetectionRegionEntry>
<regionID>{rid}</regionID>
<sensitivityLevel>50</sensitivityLevel>
<RegionCoordinatesList></RegionCoordinatesList>
{target}
</DetectionRegionEntry>
</DetectionRegionList>"""

REALM, NONCE = "IP Camera(C1234)", os.urandom(8).hex()


def alert_xml(etype: str, state: str = "active", rid: int | None = None, target: str | None = None,
              ch: int = 1) -> bytes:
    regions = ""
    if rid is not None or target:
        regions = REGION.format(rid=rid or 1,
                                target=f"<detectionTarget>{target}</detectionTarget>" if target else "")
    return ALERT.format(ch=ch, dt=time.strftime("%Y-%m-%dT%H:%M:%S+08:00"), etype=etype,
                        state=state, regions=regions).encode()


def parse_cmd(line: str) -> bytes | None:
    p = line.split()
    if not p:
        return None
    cmd, args = p[0].lower(), p[1:]
    if cmd == "vmd":
        if args and args[0] == "off":
            return alert_xml("VMD", "inactive")
        return alert_xml("VMD", rid=1 if args else None, target=args[0] if args else None)
    names = {"line": "linedetection", "field": "fielddetection", "loiter": "loitering",
             "enter": "regionEntrance", "tamper": "shelteralarm"}
    if cmd in names:
        rid = int(args[0]) if args else 1
        tgt = args[1] if len(args) > 1 else None
        return alert_xml(names[cmd], rid=rid, target=tgt)
    return None


class FakeHik:
    def __init__(self, user: str, password: str, jpeg: bytes) -> None:
        self.user, self.password, self.jpeg = user, password, jpeg
        self.streams: set[asyncio.StreamWriter] = set()

    def _auth_ok(self, method: str, auth: str | None) -> bool:
        if not auth or not auth.startswith("Digest "):
            return False
        f = dict((k, a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', auth[7:]))
        H = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1 = H(f"{self.user}:{REALM}:{self.password}")
        ha2 = H(f"{method}:{f.get('uri')}")
        exp = H(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}") if f.get("qop") \
            else H(f"{ha1}:{f['nonce']}:{ha2}")
        return f.get("response") == exp and f.get("username") == self.user

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = (await reader.readline()).decode()
            method, path, _ = line.split(" ", 2)
            headers = {}
            while (h := await reader.readline()) not in (b"\r\n", b""):
                k, _, v = h.decode().partition(":")
                headers[k.strip().lower()] = v.strip()
            if not self._auth_ok(method, headers.get("authorization")):
                writer.write((f'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest qop="auth", '
                              f'realm="{REALM}", nonce="{NONCE}", stale="FALSE"\r\nContent-Length: 0\r\n\r\n').encode())
                await writer.drain()
                writer.close()
                return
            if path.startswith("/ISAPI/Event/notification/alertStream"):
                writer.write(b"HTTP/1.1 200 OK\r\nConnection: keep-alive\r\n"
                             b"Content-Type: multipart/mixed; boundary=boundary\r\n\r\n")
                await writer.drain()
                self.streams.add(writer)
                print(f"[fake-hik] alertStream client {writer.get_extra_info('peername')}", flush=True)
                await reader.read()   # until client closes
                self.streams.discard(writer)
            elif "/picture" in path:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n"
                             % len(self.jpeg) + self.jpeg)
                await writer.drain()
                writer.close()
            else:
                writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
                writer.close()
        except (ConnectionError, ValueError):
            writer.close()

    def emit(self, xml: bytes) -> None:
        part = (b"--boundary\r\nContent-Type: application/xml; charset=\"UTF-8\"\r\n"
                b"Content-Length: %d\r\n\r\n" % len(xml)) + xml + b"\r\n"
        for w in list(self.streams):
            try:
                w.write(part)
            except Exception:  # noqa: BLE001
                self.streams.discard(w)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(10)
            self.emit(alert_xml("videoloss", "inactive"))


async def amain(a) -> None:
    jpeg = open(a.jpeg, "rb").read() if a.jpeg else b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"
    hik = FakeHik(a.user, a.password, jpeg)
    srv = await asyncio.start_server(hik.handle, a.host, a.port)
    asyncio.create_task(hik.heartbeat())
    print(f"[fake-hik] listening on {a.host}:{a.port}", flush=True)
    if a.script:
        for cmd in a.script.split(";"):
            cmd = cmd.strip()
            if cmd.startswith("sleep"):
                await asyncio.sleep(float(cmd.split()[1]))
            elif (x := parse_cmd(cmd)):
                print(f"[fake-hik] emit {cmd}", flush=True)
                hik.emit(x)
        await asyncio.sleep(a.linger)
        srv.close()
        return
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, input, "hik> ")
        x = parse_cmd(line)
        if x:
            hik.emit(x)
        else:
            print(__doc__)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="hik12345")
    ap.add_argument("--jpeg")
    ap.add_argument("--script")
    ap.add_argument("--linger", type=float, default=5)
    asyncio.run(amain(ap.parse_args()))
