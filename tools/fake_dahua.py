#!/usr/bin/env python3
"""Fake Dahua camera for testing: Digest auth, event stream, snapshot, config tables, SD card search.

Interactive: type commands on stdin to emit events, e.g.
    motion              VideoMotion Start
    motion off          VideoMotion Stop
    human               SmartMotionHuman Start
    human off           SmartMotionHuman Stop
    line 1 Human        CrossLineDetection Pulse, rule 1, Human
    region 2 Vehicle    CrossRegionDetection Start, rule 2, Vehicle
    wander 1 Human      WanderDetection Pulse
    blind               VideoBlind Start
Or script with --script "motion;sleep 2;line 1 Human".
"""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import os
import re
import time
from urllib.parse import parse_qsl, urlsplit

REALM, NONCE = "Login to FAKE0123", os.urandom(8).hex()


def event_text(code: str, action: str = "Start", data: dict | None = None, index: int = 0) -> bytes:
    text = f"Code={code};action={action};index={index}"
    if data is not None:
        text += ";data=" + json.dumps(data, indent=3)
    return text.encode()


def ivs_data(rule: int, target: str | None) -> dict:
    data: dict = {"Action": "Cross", "Direction": "Enter", "Name": f"Rule{rule}", "RuleId": rule, "UTC": 1793000000}
    if target:
        data["Object"] = {"Action": "Appear", "BoundingBox": [1, 2, 3, 4], "ObjectID": 7, "ObjectType": target}
    return data


def parse_cmd(line: str) -> bytes | None:
    p = line.split()
    if not p:
        return None
    cmd, args = p[0].lower(), p[1:]
    off = args[:1] == ["off"]
    simple = {"motion": "VideoMotion", "human": "SmartMotionHuman", "vehicle": "SmartMotionVehicle",
              "blind": "VideoBlind"}
    if cmd in simple:
        return event_text(simple[cmd], "Stop" if off else "Start")
    smart = {"line": ("CrossLineDetection", "Pulse"), "region": ("CrossRegionDetection", "Start"),
             "wander": ("WanderDetection", "Pulse")}
    if cmd in smart:
        code, action = smart[cmd]
        return event_text(code, action, ivs_data(int(args[0]) if args else 1, args[1] if len(args) > 1 else None))
    return None


def default_tables() -> dict[str, str]:
    t = {}
    for fmt, w, h, fps, rate, comp, audio in (("MainFormat", 2688, 1520, 25, 4096, "H.265", "true"),
                                              ("ExtraFormat", 704, 576, 15, 512, "H.264", "false")):
        pre = f"Encode[0].{fmt}[0]"
        t |= {f"{pre}.AudioEnable": audio, f"{pre}.Video.BitRate": str(rate), f"{pre}.Video.BitRateControl": "VBR",
              f"{pre}.Video.Compression": comp, f"{pre}.Video.FPS": str(fps), f"{pre}.Video.Height": str(h),
              f"{pre}.Video.Width": str(w)}
    for profile in range(3):
        t |= {f"VideoColor[0][{profile}].Brightness": "50", f"VideoColor[0][{profile}].Contrast": "50",
              f"VideoColor[0][{profile}].Saturation": "50", f"VideoInSharpness[0][{profile}].Sharpness": "50",
              f"VideoInDayNight[0][{profile}].Mode": "Brightness"}
    t |= {"VideoInOptions[0].Mirror": "false", "VideoInOptions[0].Flip": "false", "BlindDetect[0].Enable": "false",
          "ChannelTitle[0].Name": "Fake Dahua"}
    for tour in range(32):
        t |= {f"PtzTour[0][{tour}].Enable": "true", f"PtzTour[0][{tour}].Name": f"Tour{tour + 1}"}
        for slot in range(64):
            t |= {f"PtzTour[0][{tour}].Presets[{slot}][0]": "-1", f"PtzTour[0][{tour}].Presets[{slot}][1]": "15",
                  f"PtzTour[0][{tour}].Presets[{slot}][2]": "7"}
    t |= {f"PtzPreset[0][{i}].Name": f"Preset{i + 1}" for i in range(512)}
    return t


CAPS = """caps.MainFormat[0].Video.BitRateOptions=1024,8192
caps.MainFormat[0].Video.FPSMax=25
caps.MainFormat[0].Video.ResolutionTypes=4M,1080P,1280x720
caps.ExtraFormat[0].Video.BitRateOptions[0]=64
caps.ExtraFormat[0].Video.BitRateOptions[1]=1024
caps.ExtraFormat[0].Video.FPSMax=15
caps.ExtraFormat[0].Video.ResolutionTypes[0]=D1
caps.ExtraFormat[0].Video.ResolutionTypes[1]=CIF
"""


class FakeDahua:
    def __init__(self, user: str, password: str, jpeg: bytes = b"\xff\xd8fake-jpeg\xff\xd9",
                 recordings: list[tuple[dt.datetime, dt.datetime, str, str]] | None = None) -> None:
        self.user, self.password, self.jpeg = user, password, jpeg
        self.tables = default_tables()
        self.recordings = recordings or []         # (start, end, flag, event)
        self.streams: set[asyncio.StreamWriter] = set()
        self.finders: dict[str, dict] = {}         # object -> {conn, items}
        self.next_object = 1000
        self.requests: list[str] = []
        self.sets: list[dict[str, str]] = []
        self.has_ptz = True
        self.ptz_position = (210.07, 27.97, 4.0)
        self.zoom_value, self.zooming, self.zoom_limit, self.zoom_time = 2500, 0, 16384, time.monotonic()
        self.focus_position = 585.07
        self.presets = [(1, "Gate"), (9, "A & B")]
        self.ptz_cmds: list[tuple] = []            # (action, code, arg1, arg2, arg3)
        self.ptz_fail_stops = 0                    # refuse this many stops

    def _auth_ok(self, method: str, auth: str | None) -> bool:
        if not auth or not auth.startswith("Digest "):
            return False
        f = dict((k, a or b) for k, a, b in re.findall(r'(\w+)=(?:"([^"]*)"|([^,\s]*))', auth[7:]))
        H = lambda s: hashlib.md5(s.encode()).hexdigest()  # noqa: E731
        ha1, ha2 = H(f"{self.user}:{REALM}:{self.password}"), H(f"{method}:{f.get('uri')}")
        exp = H(f"{ha1}:{f['nonce']}:{f['nc']}:{f['cnonce']}:{f['qop']}:{ha2}")
        return f.get("response") == exp

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while (line := (await reader.readline()).decode()):
                method, path, _ = line.split(" ", 2)
                headers = {}
                while (h := await reader.readline()) not in (b"\r\n", b""):
                    k, _, v = h.decode().partition(":")
                    headers[k.strip().lower()] = v.strip()
                if not self._auth_ok(method, headers.get("authorization")):
                    writer.write((f'HTTP/1.1 401 Unauthorized\r\nWWW-Authenticate: Digest realm="{REALM}", '
                                  f'qop="auth", nonce="{NONCE}"\r\nContent-Length: 0\r\n\r\n').encode())
                    await writer.drain()
                    continue
                self.requests.append(path)
                if path.startswith("/cgi-bin/eventManager.cgi"):
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=myboundary"
                                 b"\r\nConnection: close\r\n\r\n")
                    await writer.drain()
                    self.streams.add(writer)
                    await reader.read()      # until the client closes
                    self.streams.discard(writer)
                    return
                status, ctype, body = self.reply(path, writer)
                if headers.get("connection", "").lower() == "keep-alive":
                    writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}"
                                 "\r\n\r\n".encode() + body)
                    await writer.drain()
                    continue
                # asked to close: end the body by closing, without Content-Length, like many Dahua models
                writer.write(f"HTTP/1.1 {status}\r\nContent-Type: {ctype}\r\nConnection: close\r\n\r\n".encode() + body)
                await writer.drain()
                return
        except (ConnectionError, ValueError):
            pass
        finally:
            for name, f in list(self.finders.items()):
                if f["conn"] is writer:
                    del self.finders[name]
            writer.close()

    def reply(self, path: str, conn: asyncio.StreamWriter) -> tuple[str, str, bytes]:
        u = urlsplit(path)
        q = dict(parse_qsl(u.query, keep_blank_values=True))
        ok = lambda t: ("200 OK", "text/plain", t.encode())  # noqa: E731
        bad = ("400 Bad Request", "text/plain", b"Error\r\nBad Request!\r\n")
        if u.path == "/cgi-bin/snapshot.cgi":
            return "200 OK", "image/jpeg", self.jpeg
        if u.path == "/cgi-bin/encode.cgi":
            return ok(CAPS)
        if u.path == "/cgi-bin/storageDevice.cgi":
            return ok("list.info[0].Name=/dev/sda\nlist.info[0].Detail[0].TotalBytes=2147483648.0\n"
                      "list.info[0].Detail[0].UsedBytes=1073741824.0\n")
        if u.path == "/cgi-bin/configManager.cgi":
            if q.get("action") == "getConfig":
                pre = q["name"] + "["
                found = [f"table.{k}={v}" for k, v in self.tables.items() if k.startswith(pre)]
                return ok("\r\n".join(found) + "\r\n") if found else bad
            del q["action"]
            if any(k not in self.tables for k in q):
                return bad
            self.tables.update(q)
            self.sets.append(q)
            return ok("OK\r\n")
        if u.path == "/cgi-bin/mediaFileFind.cgi":
            return self.find(q, conn, ok, bad)
        if u.path == "/cgi-bin/ptz.cgi":
            return self.ptz(q, ok, bad)
        return "404 Not Found", "text/plain", b""

    def _zoom_update(self) -> None:
        now = time.monotonic()
        moved = self.zooming * (now - self.zoom_time)
        self.zoom_value = round(max(0, min(self.zoom_limit, self.zoom_value + moved)))
        self.zoom_time = now

    def ptz(self, q: dict[str, str], ok, bad):
        action = q.get("action")
        if not self.has_ptz:
            return bad
        if action == "getCurrentProtocolCaps":
            return ok("caps.Pan=true\ncaps.Tile=true\ncaps.Zoom=true\ncaps.Preset=true\ncaps.Tour=true\ncaps.TourMax=8\n"
                      "caps.ZoomMin=1\ncaps.ZoomMax=32\n")
        if action == "getStatus":
            p = self.ptz_position
            self._zoom_update()
            return ok(f"status.MoveStatus=Idle\nstatus.Postion[0]={p[0]}\nstatus.Postion[1]={p[1]}\n"
                      f"status.Postion[2]={p[2]}\nstatus.ZoomMapValue={self.zoom_value}\n"
                      f"status.Focus.FocusPosition={self.focus_position}\n")
        if action == "getPresets":
            return ok("".join(f"presets[{i}].Index={n}\npresets[{i}].Name={name}\n"
                              for i, (n, name) in enumerate(self.presets)))
        if action in ("start", "stop"):
            if self.ptz_fail_stops and action == "stop":
                self.ptz_fail_stops -= 1
                return bad
            self.ptz_cmds.append((action, q["code"], q["arg1"], q["arg2"], q["arg3"]))
            if q["code"] == "SetPreset":
                self.presets = sorted({*self.presets, (int(q["arg2"]), f"Preset{q['arg2']}")})
            elif q["code"] == "ClearPreset":
                self.presets = [p for p in self.presets if p[0] != int(q["arg2"])]
            if q["code"] in ("ZoomTele", "ZoomWide"):
                self._zoom_update()
                rate = 4500 if q["code"] == "ZoomTele" else -4500
                self.zooming = rate if action == "start" else 0
            return ok("OK\r\n")
        return bad

    def find(self, q: dict[str, str], conn: asyncio.StreamWriter, ok, bad):
        action = q.get("action")
        if action == "factory.create":
            self.next_object += 1
            self.finders[str(self.next_object)] = {"conn": conn, "items": []}
            return ok(f"result={self.next_object}\r\n")
        f = self.finders.get(q.get("object", ""))
        if f is None or f["conn"] is not conn:   # objects belong to the connection that made them
            return bad
        if action == "findFile":
            if q.get("condition.Channel") != "1" or q.get("condition.Types[0]") != "dav":
                return bad
            start = dt.datetime.strptime(q["condition.StartTime"], "%Y-%m-%d %H:%M:%S")
            end = dt.datetime.strptime(q["condition.EndTime"], "%Y-%m-%d %H:%M:%S")
            f["items"] = [r for r in self.recordings if r[1] > start and r[0] < end]
            return ok("OK\r\n") if f["items"] else bad
        if action == "findNextFile":
            page, f["items"] = f["items"][:int(q["count"])], f["items"][int(q["count"]):]
            lines = [f"found={len(page)}"]
            for i, (s, e, flag, event) in enumerate(page):
                lines += [f"items[{i}].Channel=0", f"items[{i}].StartTime={s:%Y-%m-%d %H:%M:%S}",
                          f"items[{i}].EndTime={e:%Y-%m-%d %H:%M:%S}", f"items[{i}].Type=dav",
                          f"items[{i}].Flags[0]={flag}"]
                if event:
                    lines.append(f"items[{i}].Events[0]={event}")
            return ok("\r\n".join(lines) + "\r\n")
        if action == "close":
            return ok("OK\r\n")
        if action == "destroy":
            del self.finders[q["object"]]
            return ok("OK\r\n")
        return bad

    def emit(self, text: bytes) -> None:
        part = b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n\r\n%s\r\n" % (len(text), text)
        for w in list(self.streams):
            try:
                w.write(part)
            except Exception:  # noqa: BLE001
                self.streams.discard(w)

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(5)
            for w in list(self.streams):
                w.write(b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nHeartbeat\r\n")


async def amain(a) -> None:
    jpeg = open(a.jpeg, "rb").read() if a.jpeg else b"\xff\xd8fake-jpeg\xff\xd9"
    now = dt.datetime.now().replace(microsecond=0)
    rec = [(now - dt.timedelta(minutes=30), now - dt.timedelta(minutes=29), "Event", "VideoMotion")]
    cam = FakeDahua(a.user, a.password, jpeg, rec)
    srv = await asyncio.start_server(cam.handle, a.host, a.port)
    asyncio.create_task(cam.heartbeat())
    print(f"[fake-dahua] listening on {a.host}:{a.port}", flush=True)
    if a.script:
        while not cam.streams:
            await asyncio.sleep(0.05)
        for cmd in a.script.split(";"):
            cmd = cmd.strip()
            if cmd.startswith("sleep"):
                await asyncio.sleep(float(cmd.split()[1]))
            elif (x := parse_cmd(cmd)):
                print(f"[fake-dahua] emit {cmd}", flush=True)
                cam.emit(x)
        await asyncio.sleep(a.linger)
        srv.close()
        return
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, input, "dahua> ")
        x = parse_cmd(line)
        if x:
            cam.emit(x)
        else:
            print(__doc__)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8081)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="dahua12345")
    ap.add_argument("--jpeg")
    ap.add_argument("--script")
    ap.add_argument("--linger", type=float, default=5)
    asyncio.run(amain(ap.parse_args()))
