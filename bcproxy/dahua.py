"""Dahua side: CGI event stream, settings and SD card recordings.

Same shape as hik.py + hikcfg.py + playback.HikRecordings: VirtualCamera and BcSession use
DahuaConfig / DahuaRecordings through the methods they share with the Hikvision classes.

Written from Dahua's HTTP API documentation and public client libraries; see the README for
what has been checked against a real camera.
"""
from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from typing import Callable
from urllib.parse import quote, urlsplit, urlunsplit

from .bc import XML_DECL
from .hik import HikAlert, HikHttp, HttpStatusError
from .hikcfg import ENC_TYPE_FROM_MODE, ENC_TYPE_TO_MODE, StreamCaps, _from_hik, _to_hik
from .playback import LIST_CACHE_SECONDS, Segment, join_types
from .settings import _body

log = logging.getLogger("dahua")

ATTACH_PATH = "/cgi-bin/eventManager.cgi?action=attach&codes=[All]&heartbeat=5"

DEFAULT_EVENT_MAP = {
    "videomotion": "motion",
    "smartmotionhuman": "motion",
    "smartmotionvehicle": "motion",
    "humantrait": "motion",
    "vehicledetect": "motion",
    "nonmotordetect": "motion",
    "crosslinedetection": "crossline",
    "crossregiondetection": "intrusion",
    "wanderdetection": "loitering",
    "videoblind": "tamper",
}
DEFAULT_TARGET_MAP = {
    "human": "people",
    "vehicle": "vehicle",
    "motorvehicle": "vehicle",
    "nonmotor": "vehicle",
    "animal": "dog_cat",
}
# these codes say the target themselves, they carry no Object
CODE_TARGETS = {"smartmotionhuman": "human", "smartmotionvehicle": "vehicle", "humantrait": "human",
                "vehicledetect": "vehicle", "nonmotordetect": "nonmotor"}

# recording's Events[] / Flags[] -> Reolink recordType
RECORD_EVENTS = {"videomotion": "md", "smartmotionhuman": "people", "smartmotionvehicle": "vehicle",
                 "humantrait": "people", "vehicledetect": "vehicle",
                 "crosslinedetection": "crossline", "crossregiondetection": "intrusion",
                 "wanderdetection": "loitering", "alarmlocal": "io"}
RECORD_FLAGS = {"timing": "sched", "manual": "manual", "event": "md"}

# ResolutionTypes names; a type may also be written as 1920x1080
RESOLUTION_NAMES = {"8M": (3840, 2160), "4K": (3840, 2160), "6M": (3072, 2048), "5M": (2592, 1944),
                    "4M": (2688, 1520), "3M": (2048, 1536), "1080P": (1920, 1080), "1.3M": (1280, 960),
                    "720P": (1280, 720), "D1": (704, 576), "VGA": (640, 480), "CIF": (352, 288),
                    "QVGA": (320, 240)}
DAYNIGHT_TO_DAHUA = {"auto": "Brightness", "color": "Color", "blackAndWhite": "BlackWhite"}
DAYNIGHT_FROM_DAHUA = {"Color": "color", "BlackWhite": "blackAndWhite"}

# Reolink PtzControl command (lower case) -> ptz.cgi code. The zoom names are not from a capture
# of the app; a command that is not here is logged, see BcSession.on_ptz.
PTZ_CODES = {"left": "Left", "right": "Right", "up": "Up", "down": "Down", "leftup": "LeftUp", "leftdown": "LeftDown",
             "rightup": "RightUp", "rightdown": "RightDown", "zoomin": "ZoomTele", "zoominc": "ZoomTele",
             "zoomout": "ZoomWide", "zoomdec": "ZoomWide"}
PTZ_DIAGONAL = {"LeftUp", "LeftDown", "RightUp", "RightDown"}
PRESET_LIMIT = 128      # the app shows ids up to this; later ones stay on the camera, hidden and untouched
SET_CHUNK = 30          # setConfig keys per request, to keep the URL a sane length


def to_dahua_speed(speed: float) -> int:
    """Reolink speed 1-64 -> Dahua 1-8."""
    return 1 + (min(64, max(1, round(speed))) - 1) * 7 // 63


# getStatus ZoomMapValue is 0 at wide and 16384 at tele (measured on a real camera).
# PositionABS cannot set the zoom there: every call zoomed in one step, whatever its argument.
# So an absolute zoom is done by zooming and reading this value, see VirtualCamera.ptz_zoom_to.
ZOOM_MAX = 16384


class DahuaHttp(HikHttp):
    """Dahua often ends a response by closing the connection, without Content-Length."""
    connection = "close"

    async def text(self, path: str) -> str:
        return (await self.get_bytes(path)).decode("utf-8", "replace")


class KeepAlive:
    """One TCP connection for several requests: a mediaFileFind object belongs to the
    connection that created it on some firmware."""

    def __init__(self, http: DahuaHttp) -> None:
        self.http = http
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None

    def close(self) -> None:
        if self.writer:
            self.writer.close()
        self.reader = self.writer = None

    async def get(self, path: str) -> str:
        http = self.http
        status, data = 0, b""
        for _attempt in range(3):       # room for a connection gone stale, then an auth challenge
            if self.writer is None:
                self.reader, self.writer = await asyncio.wait_for(
                    asyncio.open_connection(http.host, http.port), http.timeout)
            try:
                r = await http.exchange(self.reader, self.writer, "GET", path, connection="keep-alive")
                data = await asyncio.wait_for(r.read_all(), http.timeout)
            except (ConnectionError, asyncio.IncompleteReadError):
                self.close()
                continue
            status = r.status
            if r.headers.get("connection", "").lower() == "close" or self.reader.at_eof():
                self.close()
            if status == 401 and "www-authenticate" in r.headers:
                http.auth.challenge(r.headers["www-authenticate"])
                continue
            break
        if status == 0:
            raise ConnectionError(f"GET {path}: connection lost")
        if status != 200:
            raise HttpStatusError(f"GET {path} -> HTTP {status}")
        return data.decode("utf-8", "replace")


def parse_kv(text: str) -> dict[str, str]:
    """key=value lines (getConfig and most other CGIs); "table." is dropped from the keys."""
    out = {}
    for line in text.splitlines():
        k, sep, v = line.partition("=")
        if sep:
            out[k.strip().removeprefix("table.")] = v.strip()
    return out


# ---------------------------------------------------------------------------
# Event stream
# ---------------------------------------------------------------------------
_EVENT = re.compile(rb"Code=([^;\r\n]+);action=([^;\r\n]+);index=(\d+)")
_PART_END = re.compile(rb"\r?\n--")
_MAX_EVENT = 1 << 20


def to_alert(code: str, action: str, index: int, data: dict | None = None) -> HikAlert:
    """One event in the form hik.EventMapper takes. index is 0-based, channel 1-based."""
    data = data if isinstance(data, dict) else {}
    objects = [data["Object"]] if isinstance(data.get("Object"), dict) else []
    if isinstance(data.get("Objects"), list):
        objects += [o for o in data["Objects"] if isinstance(o, dict)]
    types = list(dict.fromkeys(str(o["ObjectType"]) for o in objects if o.get("ObjectType")))
    rule = data.get("RuleId")
    rule = rule if isinstance(rule, int) and not isinstance(rule, bool) else None
    regions: list[tuple[int | None, str | None]] = [(rule, t) for t in types]
    if not regions and rule is not None:
        regions = [(rule, None)]
    state = {"start": "active", "stop": "inactive"}.get(action.lower(), action.lower())
    return HikAlert(event_type=code, state=state, channel=index + 1, regions=regions,
                    target=CODE_TARGETS.get(code.lower()), raw=json.dumps(data) if data else "")


class EventParser:
    """Finds events in the attach stream by their text, not by multipart framing: firmware differs
    (no blank line after the part headers, Content-Length that does not match)."""

    def __init__(self) -> None:
        self.buf = b""

    def feed(self, chunk: bytes) -> list[HikAlert]:
        self.buf += chunk
        out = []
        while True:
            m = _EVENT.search(self.buf)
            if not m:
                self.buf = self.buf[-256:]              # may hold the beginning of an event
                return out
            rest = self.buf[m.end():]
            data = None
            if not rest or (rest[:1] == b";" and len(rest) < 6):
                return out                              # may continue in the next chunk
            if rest.startswith(b";data="):
                text = rest[6:].decode("utf-8", "replace").lstrip()
                try:
                    data, end = json.JSONDecoder().raw_decode(text)
                    rest = text[end:].encode("utf-8")
                except json.JSONDecodeError:
                    # cut by the chunk, unless the part is over: then it is just not JSON
                    if not (_PART_END.search(rest) or _EVENT.search(rest) or len(rest) > _MAX_EVENT):
                        return out
            self.buf = rest
            out.append(to_alert(m[1].decode("latin1"), m[2].decode("latin1"), int(m[3]), data))


class EdgeTracker:
    """Dahua reports Start once and Stop once; the proxy's state expires unless an active alert keeps
    coming, as it does from Hikvision. So running events are repeated until they stop.

    Stop of plain motion clears the state at once. Other stops only end the repetition: smart events
    often stop right after they start, and the hold time is what lets the NVR react to them."""

    def __init__(self, stale_after: float = 600) -> None:
        self.stale_after = stale_after       # no Stop seen for this long: it got lost
        self.active: dict[tuple, tuple[HikAlert, float]] = {}

    @staticmethod
    def _key(a: HikAlert) -> tuple:
        return a.event_type.lower(), a.channel, a.regions[0][0] if a.regions else None

    def feed(self, a: HikAlert, now: float | None = None) -> HikAlert | None:
        """The alert to deliver now, if any."""
        now = time.monotonic() if now is None else now
        key = self._key(a)
        if a.state != "inactive":
            if a.state == "active":
                self.active[key] = (a, now)
            return a
        if key[2] is None:                   # a Stop without data ends every rule of this code
            for k in [k for k in self.active if k[:2] == key[:2]]:
                del self.active[k]
        else:
            self.active.pop(key, None)
        others = any(k[0] in CODE_TARGETS for k in self.active)
        return a if key[0] == "videomotion" and not others else None

    def running(self, now: float | None = None) -> list[HikAlert]:
        now = time.monotonic() if now is None else now
        for k in [k for k, (_a, t) in self.active.items() if now - t > self.stale_after]:
            del self.active[k]
        return [a for a, _t in self.active.values()]


async def _repeat(tracker: EdgeTracker, on_alert: Callable[[HikAlert], None], interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        for a in tracker.running():
            on_alert(a)


async def event_stream(http: DahuaHttp, on_alert: Callable[[HikAlert], None], idle_timeout: float = 30,
                       repeat_interval: float = 1) -> None:
    """Run forever, reconnecting on errors. The heartbeat asked for keeps a quiet stream alive."""
    backoff = 1.0
    while True:
        r = repeater = None
        try:
            r = await http.request("GET", ATTACH_PATH)
            if r.status != 200:
                raise ConnectionError(f"eventManager HTTP {r.status}")
            log.info("event stream connected (%s)", r.headers.get("content-type", ""))
            backoff = 1.0
            parser, tracker = EventParser(), EdgeTracker()
            repeater = asyncio.create_task(_repeat(tracker, on_alert, repeat_interval))
            it = r.body_iter().__aiter__()
            while True:
                chunk = await asyncio.wait_for(it.__anext__(), idle_timeout)
                for alert in parser.feed(chunk):
                    if (out := tracker.feed(alert)):
                        on_alert(out)
        except (StopAsyncIteration, asyncio.TimeoutError, EOFError, OSError, ValueError) as e:
            log.warning("event stream: %s; reconnecting in %.0fs", e or type(e).__name__, backoff)
        finally:
            if repeater:
                repeater.cancel()
            if r:
                r.close()
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------
def parse_stream_caps(text: str) -> dict[int, StreamCaps]:
    """encode.cgi getConfigCaps -> {1: main, 2: sub}. Prefix and list style differ by firmware
    (ResolutionTypes=a,b or ResolutionTypes[0]=a), so keys are matched by what they end with."""
    out: dict[int, StreamCaps] = {}
    kv = parse_kv(text)
    for idx, fmt in ((1, "MainFormat"), (2, "ExtraFormat")):
        sizes: set[tuple[int, int]] = set()
        rates: list[int] = []
        caps = StreamCaps()
        for k, v in kv.items():
            if f"{fmt}[0].Video." not in k:
                continue
            field = k.rsplit(".", 1)[1]
            if field.startswith("ResolutionTypes"):
                for name in v.split(","):
                    name = name.strip()
                    m = re.fullmatch(r"(\d+)[xX*](\d+)", name)
                    size = (int(m[1]), int(m[2])) if m else RESOLUTION_NAMES.get(name.upper())
                    if size:
                        sizes.add(size)
            elif field == "FPSMax":
                caps.max_fps = int(float(v))
            elif field.startswith("BitRateOptions"):
                rates += [int(x) for x in v.split(",") if x.strip().isdigit()]
        if rates:
            caps.max_kbps = max(rates)
        caps.sizes = sorted(sizes, reverse=True)
        if sizes:
            out[idx] = caps
    return out


class DahuaConfig:
    """Written settings go to Dahua's config tables, one key at a time. Image keys are only written
    when the camera has listed them. Recording settings and server ports are not connected."""
    unhandled_msgs = frozenset({37, 38, 54, 55, 81, 82})

    def __init__(self, http: DahuaHttp, channel: int | None = 1) -> None:
        self.http = http
        self.channel = channel or 1
        self.ch = self.channel - 1
        self.caps: dict[int, StreamCaps] = {}
        self.bitrate_mode: dict[int, str] = {}
        self.light_on_mode: str | None = None     # manual light is not connected
        self.has_tamper = False
        self.ptz_type = 0                         # Reolink ptzType: 0 none, 1 zoom only, 2 pan + tilt + zoom
        self.has_presets = False
        self.has_tours = False
        self.tour_max = 0

    async def _get(self, name: str) -> dict[str, str]:
        return parse_kv(await self.http.text(f"/cgi-bin/configManager.cgi?action=getConfig&name={name}"))

    async def _get_optional(self, name: str) -> dict[str, str]:
        """A table this model does not have is not an error."""
        try:
            return await self._get(name)
        except HttpStatusError as e:
            log.debug("no %s: %s", name, e)
            return {}

    async def _set(self, values: dict[str, object]) -> None:
        query = "&".join(f"{k}={quote(str(v), safe='')}" for k, v in values.items())
        text = await self.http.text(f"/cgi-bin/configManager.cgi?action=setConfig&{query}")
        if text.strip() != "OK":
            raise ConnectionError(f"setConfig {list(values)} -> {text[:200]!r}")

    def _stream_key(self, idx: int, field: str) -> str:
        return f"Encode[{self.ch}].{'MainFormat' if idx == 1 else 'ExtraFormat'}[0].{field}"

    async def load(self) -> None:
        try:
            self.caps = parse_stream_caps(
                await self.http.text(f"/cgi-bin/encode.cgi?action=getConfigCaps&channel={self.channel}"))
        except HttpStatusError as e:      # only the current size is offered then
            log.warning("stream capabilities: %s", e)
        enc = await self._get_optional("Encode")
        for idx in (1, 2):
            mode = enc.get(self._stream_key(idx, "Video.BitRateControl"))
            if mode:
                self.bitrate_mode[idx] = mode.lower()
        self.has_tamper = f"BlindDetect[{self.ch}].Enable" in await self._get_optional("BlindDetect")
        await self._load_ptz()

    async def _load_ptz(self) -> None:
        try:
            caps = parse_kv(await self.http.text(
                f"/cgi-bin/ptz.cgi?action=getCurrentProtocolCaps&channel={self.channel}"))
        except HttpStatusError as e:      # fixed camera
            log.debug("no PTZ: %s", e)
            return
        on = lambda k: caps.get(f"caps.{k}", "").lower() == "true"  # noqa: E731
        self.ptz_type = 2 if on("Pan") and on("Tile") else 1 if on("Zoom") else 0   # "Tile": sic
        self.has_presets = bool(self.ptz_type) and on("Preset")
        self.has_tours = bool(self.ptz_type) and on("Tour")
        self.tour_max = int(float(caps.get("caps.TourMax", 0)))

    # ----------------------------------------------------------------- status
    async def cpu_usage(self) -> int | None:
        return None                       # no plain CGI for it

    async def bitrates(self) -> tuple[int, int]:
        enc = await self._get("Encode")
        rate = lambda i: int(float(enc.get(self._stream_key(i, "Video.BitRate"), 0)))  # noqa: E731
        return rate(1), rate(2)

    async def set_channel_name(self, name: str) -> None:
        await self._set({f"ChannelTitle[{self.ch}].Name": name})

    # ---------------------------------------------------------------- streams
    async def audio_enabled(self) -> bool:
        return (await self._get("Encode")).get(self._stream_key(1, "AudioEnable"), "").lower() == "true"

    async def stream_state(self, idx: int) -> tuple[str, int, int, int, int]:
        enc = await self._get("Encode")
        get = lambda f: enc.get(self._stream_key(idx, f"Video.{f}"), "0")  # noqa: E731
        return ("H265" if "265" in get("Compression") else "H264", int(get("Width")), int(get("Height")),
                int(float(get("FPS"))), int(float(get("BitRate"))))

    async def set_stream(self, idx: int, width: int, height: int, fps: int, kbps: int,
                         audio: bool | None = None) -> None:
        caps = self.caps.get(idx)
        if caps and caps.sizes and (width, height) not in caps.sizes:
            raise ValueError(f"{width}x{height} not offered by Dahua: {caps.sizes}")
        values: dict[str, object] = {self._stream_key(idx, f"Video.{f}"): v for f, v in (
            ("Width", width), ("Height", height), ("FPS", fps), ("BitRate", kbps))}
        if audio is not None:
            values[self._stream_key(idx, "AudioEnable")] = str(audio).lower()
        await self._set(values)

    async def set_bitrate_mode(self, mode: str) -> None:
        values = {self._stream_key(i, "Video.BitRateControl"): mode.upper()
                  for i in (1, 2) if self.bitrate_mode.get(i, "vbr") != mode}
        if values:
            await self._set(values)
            self.bitrate_mode.update({1: mode, 2: mode})

    # ------------------------------------------------------------------ image
    # Reolink element -> Dahua table + field. These tables have one entry per day/night/normal
    # profile: reads use the first, writes all of them, so the page holds in every profile.
    LEVELS = (("VideoInput/bright", "VideoColor", "Brightness"), ("VideoInput/contrast", "VideoColor", "Contrast"),
              ("VideoInput/saturation", "VideoColor", "Saturation"),
              ("VideoInput/sharpen", "VideoInSharpness", "Sharpness"))

    async def _image_tables(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for name in ("VideoColor", "VideoInSharpness", "VideoInOptions", "VideoInDayNight"):
            out.update(await self._get_optional(name))
        return out

    def _profile_keys(self, d: dict[str, str], table: str, field: str) -> list[str]:
        pat = re.compile(rf"{table}\[{self.ch}\]\[\d+\]\.{field}")
        return sorted(k for k in d if pat.fullmatch(k))

    async def image_xml(self, template: bytes, frame_rate_mode: int = 0) -> bytes:
        """26/132 reply: the template's page layout with the values Dahua has."""
        d = await self._image_tables()
        values: dict[str, object] = {"InputAdvanceCfg/constantFrameRate": frame_rate_mode,
                                     "InputAdvanceCfg/encType": ENC_TYPE_FROM_MODE.get(
                                         self.bitrate_mode.get(1, "vbr"), 1)}
        for path, table, field in self.LEVELS:
            if (keys := self._profile_keys(d, table, field)):
                values[path] = _from_hik(int(float(d[keys[0]])))
        for path, field in (("InputAdvanceCfg/mirror", "Mirror"), ("InputAdvanceCfg/flip", "Flip")):
            key = f"VideoInOptions[{self.ch}].{field}"
            if key in d:
                values[path] = 1 if d[key].lower() == "true" else 0
        if (keys := self._profile_keys(d, "VideoInDayNight", "Mode")):
            values["InputAdvanceCfg/DayNight/mode"] = DAYNIGHT_FROM_DAHUA.get(d[keys[0]], "auto")
        root = _body(template)
        for path, value in values.items():
            el = root.find(path)
            if el is not None:
                el.text = str(value)
        return XML_DECL.encode() + ET.tostring(root, encoding="unicode").encode() + b"\n"

    async def apply_image(self, payload: bytes) -> list[str]:
        """25: write what changed. Returns the parts that were updated. Everything else on the page
        (anti-flicker, white balance, HDR, noise reduction, exposure) is not connected."""
        root = _body(payload)
        d = await self._image_tables()
        changes: dict[str, object] = {}
        changed: list[str] = []
        for path, table, field in self.LEVELS:
            text = root.findtext(path)
            if text in (None, ""):
                continue
            for key in self._profile_keys(d, table, field):
                if _to_hik(int(text)) != int(float(d[key])):
                    changes[key] = _to_hik(int(text))
                    if field not in changed:
                        changed.append(field)
        for path, field in (("InputAdvanceCfg/mirror", "Mirror"), ("InputAdvanceCfg/flip", "Flip")):
            text, key = root.findtext(path), f"VideoInOptions[{self.ch}].{field}"
            if text not in (None, "") and key in d and (text != "0") != (d[key].lower() == "true"):
                changes[key] = str(text != "0").lower()
                changed.append(field)
        mode = DAYNIGHT_TO_DAHUA.get(root.findtext("InputAdvanceCfg/DayNight/mode") or "")
        if mode:
            keys = [k for k in self._profile_keys(d, "VideoInDayNight", "Mode") if d[k] != mode]
            changes.update({k: mode for k in keys})
            if keys:
                changed.append("DayNight")
        if changes:
            await self._set(changes)
        enc_type = root.findtext("InputAdvanceCfg/encType")
        bitrate = ENC_TYPE_TO_MODE.get(int(enc_type)) if enc_type else None
        if bitrate and bitrate != self.bitrate_mode.get(1, "vbr"):
            await self.set_bitrate_mode(bitrate)
            changed.append(f"bitrate mode {bitrate}")
        return changed

    # -------------------------------------------------------------------- PTZ
    async def _ptz(self, action: str, code: str, a1: object = 0, a2: object = 0, a3: object = 0,
                   a4: object | None = None) -> None:
        extra = "" if a4 is None else f"&arg4={a4}"
        text = await self.http.text(f"/cgi-bin/ptz.cgi?action={action}&channel={self.channel}&code={code}"
                                    f"&arg1={a1}&arg2={a2}&arg3={a3}{extra}")
        if text.strip() != "OK":
            raise ConnectionError(f"ptz {action} {code} -> {text[:200]!r}")

    async def ptz_start(self, code: str, speed: int) -> None:
        """Keeps moving until ptz_stop. Diagonals take a vertical speed too."""
        await self._ptz("start", code, speed if code in PTZ_DIAGONAL else 0, speed)

    async def ptz_stop(self, code: str) -> None:
        await self._ptz("stop", code)

    async def ptz_goto(self, preset: int) -> None:
        await self._ptz("start", "GotoPreset", 0, preset)

    async def ptz_presets(self) -> list[tuple[int, str]]:
        kv = parse_kv(await self.http.text(f"/cgi-bin/ptz.cgi?action=getPresets&channel={self.channel}"))
        return sorted((int(v), kv.get(f"presets[{m[1]}].Name", ""))
                      for k, v in kv.items()
                      if (m := re.fullmatch(r"presets\[(\d+)\]\.Index", k)) and int(v) <= PRESET_LIMIT)

    async def ptz_preset_save(self, preset: int, name: str | None) -> int:
        """The camera's current position under this id; an existing preset of that id is replaced.
        Id 0 is how the app asks for a new preset: it takes the lowest free id. Returns the id used."""
        if preset == 0:
            used = {i for i, _name in await self.ptz_presets()}
            preset = next((i for i in range(1, PRESET_LIMIT + 1) if i not in used), 0)
            if not preset:
                raise ValueError(f"all presets 1-{PRESET_LIMIT} are in use")
        if not 1 <= preset <= PRESET_LIMIT:
            raise ValueError(f"preset id {preset} outside 1-{PRESET_LIMIT}")
        await self._ptz("start", "SetPreset", 0, preset)
        if name:
            await self._set({f"PtzPreset[{self.ch}][{preset - 1}].Name": name})
        return preset

    async def ptz_preset_delete(self, preset: int) -> None:
        if not 1 <= preset <= PRESET_LIMIT:
            raise ValueError(f"preset id {preset} outside 1-{PRESET_LIMIT}")
        await self._ptz("start", "ClearPreset", 0, preset)

    # ------------------------------------------------------------------ tours (Reolink: patrol)
    async def ptz_tours(self) -> list[dict]:
        """Tours that have points. Empty ones are free slots, they are not patrols yet.
        The table holds a preset's index, its id - 1, and -1 for no point: AddTour 7 stores 6 (real camera)."""
        kv = await self._get("PtzTour")
        tours = []
        for i in range(self.tour_max):
            pre = f"PtzTour[{self.ch}][{i}]"
            points = []
            for m in sorted((m for k in kv if (m := re.fullmatch(re.escape(pre) + r"\.Presets\[(\d+)\]\[0\]", k))),
                            key=lambda m: int(m[1])):
                index = int(float(kv[m[0]]))
                if index >= 0:
                    points.append({"preset": index + 1, "dwell": int(float(kv.get(f"{pre}.Presets[{m[1]}][1]", 15))),
                                   "speed": int(float(kv.get(f"{pre}.Presets[{m[1]}][2]", 7)))})
            if points:
                tours.append({"id": i + 1, "name": kv.get(f"{pre}.Name", f"Tour{i + 1}"),
                              "enable": kv.get(f"{pre}.Enable", "true").lower() == "true", "points": points})
        return tours

    async def ptz_tour_save(self, tour: int, name: str | None, enable: bool, points: list[dict]) -> None:
        """Points beyond the new list are cleared. Only keys the camera has are written."""
        if not 1 <= tour <= self.tour_max:
            raise ValueError(f"tour {tour} outside 1-{self.tour_max}")
        kv = await self._get("PtzTour")
        pre = f"PtzTour[{self.ch}][{tour - 1}]"
        slots = sorted(int(m[1]) for k in kv if (m := re.fullmatch(re.escape(pre) + r"\.Presets\[(\d+)\]\[0\]", k)))
        if len(points) > len(slots):
            raise ValueError(f"patrol has {len(points)} points, the camera allows {len(slots)}")
        values: dict[str, object] = {f"{pre}.Enable": str(enable).lower()}
        if name:
            values[f"{pre}.Name"] = name
        for j, slot in enumerate(slots):
            if j < len(points):
                p = points[j]
                values |= {f"{pre}.Presets[{slot}][0]": p["preset"] - 1, f"{pre}.Presets[{slot}][1]": p["dwell"],
                           f"{pre}.Presets[{slot}][2]": p["speed"]}
            elif float(kv[f"{pre}.Presets[{slot}][0]"]) >= 0:
                values[f"{pre}.Presets[{slot}][0]"] = -1
        items = list(values.items())
        for i in range(0, len(items), SET_CHUNK):
            await self._set(dict(items[i:i + SET_CHUNK]))

    async def ptz_tour_start(self, tour: int) -> None:
        await self._ptz("start", "StartTour", tour)

    async def ptz_tour_stop(self, tour: int) -> None:
        await self._ptz("start", "StopTour", tour)

    async def ptz_status(self) -> dict[str, str]:
        return parse_kv(await self.http.text(f"/cgi-bin/ptz.cgi?action=getStatus&channel={self.channel}"))

    async def zoom_focus(self) -> tuple[int, int]:
        """(zoom, focus) positions. ZoomMapValue is the zoom's place from wide (0) to tele (ZOOM_MAX)."""
        s = await self.ptz_status()
        return int(float(s.get("status.ZoomMapValue", 0))), int(float(s.get("status.Focus.FocusPosition", 0)))

    # ----------------------------------------------------------------- tamper
    async def tamper_enabled(self) -> bool:
        return (await self._get("BlindDetect")).get(f"BlindDetect[{self.ch}].Enable", "").lower() == "true"

    async def set_tamper(self, enable: bool) -> None:
        await self._set({f"BlindDetect[{self.ch}].Enable": str(enable).lower()})


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------
def _time(t: dt.datetime) -> str:
    return quote(t.strftime("%Y-%m-%d %H:%M:%S"), safe="-:")


def parse_found(text: str) -> list[Segment]:
    """findNextFile reply -> recordings."""
    items: dict[int, dict] = {}
    for k, v in parse_kv(text).items():
        m = re.fullmatch(r"items\[(\d+)\]\.(\w+)(\[\d+\])?", k)
        if not m:
            continue
        item = items.setdefault(int(m[1]), {})
        if m[3]:
            item.setdefault(m[2], []).append(v)
        else:
            item[m[2]] = v
    out = []
    for item in items.values():
        try:
            start = dt.datetime.strptime(item["StartTime"], "%Y-%m-%d %H:%M:%S")
            end = dt.datetime.strptime(item["EndTime"], "%Y-%m-%d %H:%M:%S")
        except (KeyError, ValueError):
            continue
        types = [RECORD_EVENTS[e.lower()] for e in item.get("Events", []) if e.lower() in RECORD_EVENTS]
        types += [RECORD_FLAGS[f.lower()] for f in item.get("Flags", []) if f.lower() in RECORD_FLAGS]
        out.append(Segment(start, end, join_types(*types) if types else "sched"))
    return out


class DahuaRecordings:
    """Dahua searches and replays in camera-local wall clock, the same as the Reolink app."""
    PAGE = 100

    def __init__(self, http: DahuaHttp, channel: int | None = 1) -> None:
        self.http, self.channel = http, channel or 1
        self._lists: dict[tuple, tuple[float, list[Segment]]] = {}
        self._days: dict[tuple[int, int], tuple[float, set[int]]] = {}

    async def storage(self) -> tuple[int, int] | None:
        """(capacity MB, free MB), all partitions together."""
        try:
            info = parse_kv(await self.http.text("/cgi-bin/storageDevice.cgi?action=getDeviceAllInfo"))
        except HttpStatusError as e:      # no card on some models
            log.debug("no storage: %s", e)
            return None
        total = sum(float(v) for k, v in info.items() if k.endswith(".TotalBytes"))
        used = sum(float(v) for k, v in info.items() if k.endswith(".UsedBytes"))
        if total <= 0:
            return None
        return int(total) >> 20, int(total - used) >> 20

    async def _find(self, conn: KeepAlive, start: dt.datetime, end: dt.datetime, limit: int) -> list[Segment]:
        cgi = "/cgi-bin/mediaFileFind.cgi?action="
        created = parse_kv(await conn.get(cgi + "factory.create"))
        obj = created.get("result")
        if not obj:
            raise ConnectionError(f"mediaFileFind factory.create -> {created}")
        out: list[Segment] = []
        try:
            try:
                await conn.get(f"{cgi}findFile&object={obj}&condition.Channel={self.channel}"
                               f"&condition.StartTime={_time(start)}&condition.EndTime={_time(end)}"
                               "&condition.Types[0]=dav&condition.VideoStream=Main")
            except HttpStatusError:   # some firmware answers an error when nothing matches
                return []
            while len(out) < limit:
                count = min(self.PAGE, limit - len(out))
                page = parse_found(await conn.get(f"{cgi}findNextFile&object={obj}&count={count}"))
                out += page
                if len(page) < count:
                    break
        finally:
            for action in ("close", "destroy"):
                try:
                    await conn.get(f"{cgi}{action}&object={obj}")
                except (OSError, asyncio.TimeoutError) as e:
                    log.debug("mediaFileFind %s: %s", action, e)
        return out

    async def search(self, start: dt.datetime, end: dt.datetime, limit: int = 1000) -> list[Segment]:
        """Recordings overlapping [start, end], oldest first. Kept for a moment, like HikRecordings:
        the app asks again on every visit."""
        key = (start, end, limit)
        cached = self._lists.get(key)
        if limit > 1 and cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]
        conn = KeepAlive(self.http)
        try:
            found = await self._find(conn, start, end, limit)
        finally:
            conn.close()
        found = sorted((s for s in found if s.end > start and s.start < end), key=lambda s: s.start)
        if limit > 1:
            self._lists = {key: (time.monotonic(), found)}
        return found

    async def record_days(self, year: int, month: int) -> set[int]:
        """Dahua has no monthly summary here: one small search per day, up to today."""
        cached = self._days.get((year, month))
        if cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]
        days: set[int] = set()
        last = dt.datetime.now() + dt.timedelta(days=1)
        day = dt.datetime(year, month, 1)
        conn = KeepAlive(self.http)
        try:
            while day.month == month and day <= last:
                if await self._find(conn, day, day + dt.timedelta(days=1), 1):
                    days.add(day.day)
                day += dt.timedelta(days=1)
        finally:
            conn.close()
        self._days[(year, month)] = (time.monotonic(), days)
        return days

    async def replay_url(self, live_rtsp: str, start: dt.datetime, end: dt.datetime) -> str:
        """rtsp://user:pass@host:554/cam/realmonitor?channel=1&subtype=0 -> /cam/playback?channel=1&starttime=..."""
        u = urlsplit(live_rtsp)
        q = f"channel={self.channel}&starttime={start:%Y_%m_%d_%H_%M_%S}&endtime={end:%Y_%m_%d_%H_%M_%S}"
        return urlunsplit((u.scheme, u.netloc, "/cam/playback", q, ""))
