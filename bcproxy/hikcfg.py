"""Settings the app changes that must take effect on the Hikvision camera.

Streams    57 Compression       <-> /ISAPI/Streaming/channels/<ch>0<1|2>
Recording  81/82 Record         <-> /ISAPI/ContentMgmt/record/tracks/<ch>01 (schedule)
           54/55 RecordCfg      <-> same track (overwrite, pre/post record time)
Light      288 FloodlightManual  -> /ISAPI/Image/channels/<ch>/supplementLight

Hikvision wants whole documents back on PUT, so the current document is read
and only the affected elements are replaced.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

from .bc import XML_DECL
from .hik import HikHttp, _strip_ns
from .settings import _body

log = logging.getLogger("hikcfg")

# Reolink schedule tables: 7 x 24 characters, Sunday 00:00 first
DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
MAX_ACTIONS_PER_DAY = 8      # Hikvision: ScheduleActionSize="8"
POST_RECORD = [5, 10, 30, 60, 120, 300, 600]
PRE_RECORD = [0, 5, 10, 15, 20, 25, 30]


def _set(doc: str, tag: str, value) -> str:
    """Replace the text of the first <tag> element (attributes kept)."""
    new, n = re.subn(rf"(<{tag}(?:\s[^>]*)?>)[^<]*(</{tag}>)", rf"\g<1>{value}\g<2>", doc, count=1)
    if not n:
        raise ValueError(f"no <{tag}> in Hikvision document")
    return new


def _check(status: int, body: bytes, what: str) -> None:
    if status != 200:
        raise ConnectionError(f"{what} -> HTTP {status}: {body[:200]!r}")
    if b"<statusCode>7</statusCode>" in body:
        log.warning("%s: Hikvision asks for a reboot to apply this", what)


@dataclass
class StreamCaps:
    sizes: list[tuple[int, int]] = field(default_factory=list)
    max_fps: int = 30
    max_kbps: int = 8192


def parse_stream_caps(doc: bytes) -> StreamCaps:
    root = ET.fromstring(doc)
    _strip_ns(root)
    video = root.find("Video")
    if video is None:
        return StreamCaps()
    w = (video.find("videoResolutionWidth").attrib.get("opt", "")).split(",")
    h = (video.find("videoResolutionHeight").attrib.get("opt", "")).split(",")
    sizes = sorted({(int(a), int(b)) for a, b in zip(w, h) if a and b}, reverse=True)
    fps = [int(x) for x in video.find("maxFrameRate").attrib.get("opt", "3000").split(",")]
    kbps = video.find("vbrUpperCap").attrib.get("max", "8192")
    return StreamCaps(sizes, max(fps) // 100, int(kbps))


def schedule_tables(track: str) -> dict[str, str]:
    """Hikvision track document -> Reolink valueTables (Normal / event)."""
    normal, event = ["0"] * 168, ["0"] * 168
    for act in re.findall(r"<ScheduleAction>.*?</ScheduleAction>", track, re.S):
        day = re.search(r"<ScheduleActionStartTime>.*?<DayOfWeek>(\w+)", act, re.S)
        tod = re.findall(r"<TimeOfDay>(\d+):(\d+)", act)
        mode = re.search(r"<ActionRecordingMode>(\w+)", act)
        if not day or len(tod) < 2 or not mode or day.group(1) not in DAYS or "<Record>true" not in act:
            continue
        first = DAYS.index(day.group(1)) * 24
        start, end = int(tod[0][0]), int(tod[1][0]) + (1 if int(tod[1][1]) else 0)
        table = normal if mode.group(1) == "CMR" else event
        table[first + start:first + end] = ["1"] * (end - start)
    return {"Normal": "".join(normal), "event": "".join(event)}


def record_xml(track: str, event_types: list[str], channel: int = 0) -> str:
    t = schedule_tables(track)
    m = re.search(r"<enableSchedule>(\w+)", track)
    enable = 0 if m and m.group(1) == "false" else 1
    items = "".join(f"<item>\n<type>{name}</type>\n<valueTable>{table}</valueTable>\n</item>\n"
                    for name, table in [("MD", t["event"]), ("Normal", t["Normal"])]
                    + [(x, t["event"]) for x in event_types])
    return (f'<Record version="1.1">\n<channelId>{channel}</channelId>\n<enable>{enable}</enable>\n'
            f"<typeScheduleList>\n{items}</typeScheduleList>\n</Record>")


def schedule_actions(tables: dict[str, str]) -> str:
    """Reolink valueTables -> <ScheduleAction> list. Continuous beats event in
    an hour selected for both; every event type maps to Hikvision's EDR."""
    hours: list[str | None] = []
    for h in range(168):
        on = {k for k, v in tables.items() if h < len(v) and v[h] == "1"}
        hours.append("CMR" if "Normal" in on else "EDR" if on else None)
    out = []
    for d, day in enumerate(DAYS):
        cells = hours[d * 24:(d + 1) * 24]
        runs, h = [], 0
        while h < 24:
            end = h
            while end < 24 and cells[end] == cells[h]:
                end += 1
            if cells[h]:
                runs.append((h, end, cells[h]))
            h = end
        if len(runs) > MAX_ACTIONS_PER_DAY:
            raise ValueError(f"{day}: {len(runs)} schedule blocks, Hikvision allows {MAX_ACTIONS_PER_DAY}")
        for i, (a, b, mode) in enumerate(runs, 1):
            out.append(f"<ScheduleAction><id>{i}</id>"
                       f"<ScheduleActionStartTime><DayOfWeek>{day}</DayOfWeek><TimeOfDay>{a:02d}:00:00</TimeOfDay>"
                       f"</ScheduleActionStartTime><ScheduleActionEndTime><DayOfWeek>{day}</DayOfWeek>"
                       f"<TimeOfDay>{b:02d}:00:00</TimeOfDay></ScheduleActionEndTime>"
                       "<ScheduleDSTEnable>false</ScheduleDSTEnable><Description>nothing</Description>"
                       f"<Actions><Record>true</Record><ActionRecordingMode>{mode}</ActionRecordingMode></Actions>"
                       "</ScheduleAction>")
    return "".join(out)


def replace_schedule(track: str, actions: str) -> str:
    first = track.find("<ScheduleAction>")
    last = track.rfind("</ScheduleAction>")
    if first < 0:
        block = re.search(r"<ScheduleBlockType>[^<]*</ScheduleBlockType>", track)
        if not block:
            raise ValueError("no ScheduleBlock in Hikvision track document")
        return track[:block.end()] + actions + track[block.end():]
    return track[:first] + actions + track[last + len("</ScheduleAction>"):]


def _nearest(options: list[int], value: int) -> int:
    return min(options, key=lambda o: abs(o - value))


def _to_hik(v: int) -> int:
    """Reolink 0-255 -> Hikvision 0-100."""
    return max(0, min(100, round(v * 100 / 255)))


def _from_hik(v: int) -> int:
    return max(0, min(255, round(v * 255 / 100)))


def _ns_doc(tag: str, inner: str) -> bytes:
    return (f'<?xml version="1.0" encoding="UTF-8"?><{tag} version="2.0" '
            f'xmlns="http://www.hikvision.com/ver20/XMLSchema">{inner}</{tag}>').encode()


def _tag(doc: str, tag: str, default: str = "") -> str:
    m = re.search(rf"<{tag}(?:\s[^>]*)?>([^<]*)</{tag}>", doc)
    return m.group(1).strip() if m else default


DAYNIGHT_TO_HIK = {"auto": "auto", "color": "day", "blackAndWhite": "night"}
DAYNIGHT_FROM_HIK = {"day": "color", "night": "blackAndWhite"}
# Reolink hdrSwitch (reolink_aio HDREnum: 0 off, 1 auto, 2 on) <-> Hikvision WDR mode
HDR_TO_HIK = {0: "close", 1: "auto", 2: "open"}
HDR_FROM_HIK = {v: k for k, v in HDR_TO_HIK.items()}
# Bitrate mode lives in the image page (25/26), not in Compression. Observed in the
# app: choosing "Variable" sends encType 0.
ENC_TYPE_TO_MODE = {0: "vbr", 1: "cbr"}
ENC_TYPE_FROM_MODE = {v: k for k, v in ENC_TYPE_TO_MODE.items()}


def _hik_bitrate(doc: str) -> int:
    vbr = re.search(r"<videoQualityControlType>\s*vbr", doc, re.I)
    m = (re.search(r"<vbrUpperCap>(\d+)</vbrUpperCap>", doc) if vbr else None) or \
        re.search(r"<constantBitRate>(\d+)</constantBitRate>", doc) or \
        re.search(r"<vbrUpperCap>(\d+)</vbrUpperCap>", doc)
    return int(m.group(1)) if m else 0


class HikConfig:
    unhandled_msgs: frozenset[int] = frozenset()    # settings messages this camera type does not take
    ptz_type = 0                                    # PTZ is not connected for Hikvision
    has_presets = False
    has_tours = False

    def __init__(self, http: HikHttp, channel: int | None = 1) -> None:
        self.http, self.channel = http, channel or 1
        self.caps: dict[int, StreamCaps] = {}     # 1 = main, 2 = sub
        self.bitrate_mode: dict[int, str] = {}    # stream -> "cbr" / "vbr"
        self.light_on_mode: str | None = None     # supplementLightMode that means "light on"
        self.has_tamper = False

    # ----------------------------------------------------------------- status
    async def cpu_usage(self) -> int | None:
        doc = (await self.http.get_bytes("/ISAPI/System/status")).decode("utf-8", "replace")
        m = re.search(r"<cpuUtilization>(\d+)</cpuUtilization>", doc)
        return int(m.group(1)) if m else None

    async def bitrates(self) -> tuple[int, int]:
        """Configured (main, sub) bitrate in kbps."""
        docs = [(await self.http.get_bytes(self._stream_path(i))).decode("utf-8", "replace") for i in (1, 2)]
        return _hik_bitrate(docs[0]), _hik_bitrate(docs[1])

    async def set_channel_name(self, name: str) -> None:
        path = f"/ISAPI/System/Video/inputs/channels/{self.channel}"
        doc = (await self.http.get_bytes(path)).decode("utf-8")
        new_doc, n = re.subn(r"<name>.*?</name>", f"<name>{xml_escape(name)}</name>", doc, count=1, flags=re.S)
        if not n:
            raise ValueError("no <name> in VideoInputChannel")
        status, body = await self.http.put_bytes(path, new_doc.encode("utf-8"))
        if status != 200:
            raise ConnectionError(f"HTTP {status}: {body[:200]!r}")

    # ---------------------------------------------------------------- streams
    def _stream_path(self, idx: int) -> str:
        return f"/ISAPI/Streaming/channels/{self.channel}0{idx}"

    async def load(self) -> None:
        for idx in (1, 2):
            try:
                self.caps[idx] = parse_stream_caps(
                    await self.http.get_bytes(self._stream_path(idx) + "/capabilities"))
                doc = (await self.http.get_bytes(self._stream_path(idx))).decode("utf-8", "replace")
                self.bitrate_mode[idx] = _tag(doc, "videoQualityControlType", "VBR").lower()
            except Exception as e:  # noqa: BLE001 - older firmware: only the current size is offered
                log.warning("stream %d capabilities: %s", idx, e)
        try:
            doc = (await self.http.get_bytes(self._light_path + "/capabilities")).decode("utf-8", "replace")
            m = re.search(r'<supplementLightMode[^>]*opt="([^"]*)"', doc)
            modes = m.group(1).split(",") if m else []
            self.light_on_mode = next((x for x in modes if "white" in x.lower()), None)
        except Exception as e:  # noqa: BLE001 - no supplement light on this model
            log.debug("no supplement light: %s", e)
        try:
            await self.tamper_enabled()
            self.has_tamper = True
        except ConnectionError as e:  # model without tamper detection
            log.debug("no tamper detection: %s", e)

    @property
    def _tamper_path(self) -> str:
        return f"/ISAPI/System/Video/inputs/channels/{self.channel}/tamperDetection"

    async def tamper_enabled(self) -> bool:
        return _tag((await self.http.get_bytes(self._tamper_path)).decode("utf-8"), "enabled") == "true"

    async def set_tamper(self, enable: bool) -> None:
        doc = (await self.http.get_bytes(self._tamper_path)).decode("utf-8")
        status, body = await self.http.put_bytes(self._tamper_path,
                                                 _set(doc, "enabled", str(enable).lower()).encode("utf-8"))
        _check(status, body, "PUT tamper detection")

    async def audio_enabled(self) -> bool:
        doc = (await self.http.get_bytes(self._stream_path(1))).decode("utf-8", "replace")
        m = re.search(r"<Audio>.*?<enabled>(\w+)</enabled>", doc, re.S)
        return bool(m) and m.group(1) == "true"

    async def stream_state(self, idx: int) -> tuple[str, int, int, int, int]:
        """(codec, width, height, fps, kbps) Hikvision really uses: it clamps what it is given."""
        doc = (await self.http.get_bytes(self._stream_path(idx))).decode("utf-8")
        codec = "H265" if "265" in _tag(doc, "videoCodecType") else "H264"
        return (codec, int(_tag(doc, "videoResolutionWidth", "0")), int(_tag(doc, "videoResolutionHeight", "0")),
                int(_tag(doc, "maxFrameRate", "0")) // 100, int(_tag(doc, "vbrUpperCap", "0")))

    async def set_stream(self, idx: int, width: int, height: int, fps: int, kbps: int,
                         audio: bool | None = None) -> None:
        caps = self.caps.get(idx)
        if caps and caps.sizes and (width, height) not in caps.sizes:
            raise ValueError(f"{width}x{height} not offered by Hikvision: {caps.sizes}")
        path = self._stream_path(idx)
        doc = (await self.http.get_bytes(path)).decode("utf-8")
        doc = _set(_set(doc, "videoResolutionWidth", width), "videoResolutionHeight", height)
        doc = _set(_set(_set(doc, "maxFrameRate", fps * 100), "constantBitRate", kbps), "vbrUpperCap", kbps)
        if audio is not None:
            doc = re.sub(r"(<Audio>.*?<enabled>)\w+(</enabled>)", rf"\g<1>{str(audio).lower()}\g<2>", doc,
                         count=1, flags=re.S)
        status, body = await self.http.put_bytes(path, doc.encode("utf-8"))
        _check(status, body, f"PUT stream {idx}")

    async def set_bitrate_mode(self, mode: str) -> None:
        """The app's bitrate mode is one setting for the camera; Hikvision has one per stream."""
        for idx in (1, 2):
            if self.bitrate_mode.get(idx, "vbr") == mode:
                continue
            path = self._stream_path(idx)
            doc = (await self.http.get_bytes(path)).decode("utf-8")
            status, body = await self.http.put_bytes(
                path, _set(doc, "videoQualityControlType", mode.upper()).encode("utf-8"))
            _check(status, body, f"PUT stream {idx} {mode}")
            self.bitrate_mode[idx] = mode
            log.info("Hikvision stream %d bitrate mode now %s", idx, mode)

    # ------------------------------------------------------------------ image
    def _image_path(self, sub: str) -> str:
        return f"/ISAPI/Image/channels/{self.channel}/{sub}"

    async def _image_docs(self) -> dict[str, str]:
        docs = {s: (await self.http.get_bytes(self._image_path(s))).decode("utf-8")
                for s in ("Color", "Sharpness", "ImageFlip", "powerLineFrequency", "IrcutFilter",
                          "WhiteBalance", "NoiseReduce")}
        try:
            docs["WDR"] = (await self.http.get_bytes(self._image_path("WDR"))).decode("utf-8")
        except ConnectionError:      # model without WDR: no HDR in the image page
            docs["WDR"] = ""
        return docs

    async def image_xml(self, template: bytes, frame_rate_mode: int = 0) -> bytes:
        """26/132 reply: the full page layout from a real camera, with Hikvision's current values."""
        d = await self._image_docs()
        style = _tag(d["ImageFlip"], "ImageFlipStyle") if _tag(d["ImageFlip"], "enabled") == "true" else ""
        wb_manual = _tag(d["WhiteBalance"], "WhiteBalanceStyle") == "manual"
        values = {
            "VideoInput/bright": _from_hik(int(_tag(d["Color"], "brightnessLevel", "50"))),
            "VideoInput/contrast": _from_hik(int(_tag(d["Color"], "contrastLevel", "50"))),
            "VideoInput/saturation": _from_hik(int(_tag(d["Color"], "saturationLevel", "50"))),
            "VideoInput/sharpen": _from_hik(int(_tag(d["Sharpness"], "SharpnessLevel", "50"))),
            "InputAdvanceCfg/PowerLineFrequency/mode": _tag(d["powerLineFrequency"], "powerLineFrequencyMode", "60hz"),
            "InputAdvanceCfg/PowerLineFrequency/enable": 1,
            "InputAdvanceCfg/DayNight/mode": DAYNIGHT_FROM_HIK.get(_tag(d["IrcutFilter"], "IrcutFilterType"), "auto"),
            "InputAdvanceCfg/Scene/mode": "manual" if wb_manual else "auto",
            "InputAdvanceCfg/Scene/Redgain/cur": _from_hik(int(_tag(d["WhiteBalance"], "WhiteBalanceRed", "50"))),
            "InputAdvanceCfg/Scene/Bluegain/cur": _from_hik(int(_tag(d["WhiteBalance"], "WhiteBalanceBlue", "50"))),
            "InputAdvanceCfg/mirror": 1 if style in ("LEFTRIGHT", "CENTER") else 0,
            "InputAdvanceCfg/flip": 1 if style in ("UPDOWN", "CENTER") else 0,
            "InputAdvanceCfg/nr3d/enable": 0 if _tag(d["NoiseReduce"], "mode") == "close" else 1,
            "InputAdvanceCfg/constantFrameRate": frame_rate_mode,
            "InputAdvanceCfg/encType": ENC_TYPE_FROM_MODE.get(self.bitrate_mode.get(1, "vbr"), 1),
        }
        root = _body(template)
        adv = root.find("InputAdvanceCfg")
        if d["WDR"] and adv is not None:      # the app offers HDR when the element exists
            ET.SubElement(adv, "hdrSwitch").text = str(HDR_FROM_HIK.get(_tag(d["WDR"], "mode"), 0))
        for path, value in values.items():
            el = root.find(path)
            if el is not None:
                el.text = str(value)
        return XML_DECL.encode() + ET.tostring(root, encoding="unicode").encode() + b"\n"

    async def apply_image(self, payload: bytes) -> list[str]:
        """25: write what changed. Returns the Hikvision parts that were updated.
        Exposure, gain, backlight and hue have no safe Hikvision mapping and are not applied."""
        root = _body(payload)
        d = await self._image_docs()
        changed: list[str] = []

        def num(path: str) -> int | None:
            text = root.findtext(path)
            return int(text) if text not in (None, "") else None

        async def put(sub: str, doc: str | bytes) -> None:
            status, body = await self.http.put_bytes(self._image_path(sub),
                                                     doc.encode("utf-8") if isinstance(doc, str) else doc)
            _check(status, body, f"PUT image {sub}")
            changed.append(sub)

        color = d["Color"]
        for path, tag in (("VideoInput/bright", "brightnessLevel"), ("VideoInput/contrast", "contrastLevel"),
                          ("VideoInput/saturation", "saturationLevel")):
            v = num(path)
            if v is not None and _to_hik(v) != int(_tag(color, tag, "50")):
                color = _set(color, tag, _to_hik(v))
        if color != d["Color"]:
            await put("Color", color)
        v = num("VideoInput/sharpen")
        if v is not None and _to_hik(v) != int(_tag(d["Sharpness"], "SharpnessLevel", "50")):
            await put("Sharpness", _set(d["Sharpness"], "SharpnessLevel", _to_hik(v)))

        mirror, flip = num("InputAdvanceCfg/mirror"), num("InputAdvanceCfg/flip")
        if mirror is not None and flip is not None:
            style = {(1, 1): "CENTER", (1, 0): "LEFTRIGHT", (0, 1): "UPDOWN"}.get((mirror, flip), "")
            on = _tag(d["ImageFlip"], "enabled") == "true"
            if (_tag(d["ImageFlip"], "ImageFlipStyle") if on else "") != style:
                inner = f"<enabled>{str(bool(style)).lower()}</enabled>"
                await put("ImageFlip", _ns_doc("ImageFlip", inner + (f"<ImageFlipStyle>{style}</ImageFlipStyle>"
                                                                     if style else "")))

        # enable=0 means anti-flicker off: its mode is then only a placeholder (the app sends 50hz)
        power = root.findtext("InputAdvanceCfg/PowerLineFrequency/mode")
        if (num("InputAdvanceCfg/PowerLineFrequency/enable") == 1 and power in ("50hz", "60hz")
                and power != _tag(d["powerLineFrequency"], "powerLineFrequencyMode")):
            await put("powerLineFrequency", _set(d["powerLineFrequency"], "powerLineFrequencyMode", power))

        ir = DAYNIGHT_TO_HIK.get(root.findtext("InputAdvanceCfg/DayNight/mode") or "")
        if ir and ir != _tag(d["IrcutFilter"], "IrcutFilterType"):
            await put("IrcutFilter", _set(d["IrcutFilter"], "IrcutFilterType", ir))

        wb = d["WhiteBalance"]
        scene = root.findtext("InputAdvanceCfg/Scene/mode")
        if scene == "manual":
            wb = _set(wb, "WhiteBalanceStyle", "manual")
            for path, tag in (("InputAdvanceCfg/Scene/Redgain/cur", "WhiteBalanceRed"),
                              ("InputAdvanceCfg/Scene/Bluegain/cur", "WhiteBalanceBlue")):
                v = num(path)
                if v is not None:
                    wb = _set(wb, tag, _to_hik(v))
        elif scene == "auto" and _tag(wb, "WhiteBalanceStyle") == "manual":
            wb = _set(wb, "WhiteBalanceStyle", "auto1")
        if wb != d["WhiteBalance"]:
            await put("WhiteBalance", wb)

        hdr = HDR_TO_HIK.get(num("InputAdvanceCfg/hdrSwitch"))
        if hdr and d["WDR"] and hdr != _tag(d["WDR"], "mode"):
            await put("WDR", _set(d["WDR"], "mode", hdr))

        mode = ENC_TYPE_TO_MODE.get(num("InputAdvanceCfg/encType"))
        if mode and mode != self.bitrate_mode.get(1, "vbr"):
            await self.set_bitrate_mode(mode)
            changed.append(f"bitrate mode {mode}")

        nr = num("InputAdvanceCfg/nr3d/enable")
        if nr is not None and bool(nr) == (_tag(d["NoiseReduce"], "mode") == "close"):
            await put("NoiseReduce", _set(d["NoiseReduce"], "mode", "general" if nr else "close"))
        return changed

    # ------------------------------------------------------------ server ports
    async def _accesses(self) -> str:
        return (await self.http.get_bytes("/ISAPI/Security/adminAccesses")).decode("utf-8")

    @staticmethod
    def _access(doc: str, protocol: str) -> tuple[bool, int]:
        for block in re.findall(r"<AdminAccessProtocol(?:\s[^>]*)?>.*?</AdminAccessProtocol>", doc, re.S):
            if _tag(block, "protocol") == protocol:
                return _tag(block, "enabled") == "true", int(_tag(block, "portNo", "0"))
        return False, 0

    async def server_ports_xml(self, bc_port: int) -> str:
        """37: Hikvision's real services. ONVIF has no port of its own, it answers on HTTP."""
        doc = await self._accesses()
        integrate = (await self.http.get_bytes("/ISAPI/System/Network/Integrate")).decode("utf-8")
        http, https, rtsp = (self._access(doc, p) for p in ("HTTP", "HTTPS", "RTSP"))
        onvif = re.search(r"<ONVIF[^>]*>.*?<enable>(\w+)", integrate, re.S)
        row = lambda tag, port_tag, port, on: (f'<{tag} version="1.1">\n<{port_tag}>{port}</{port_tag}>\n'  # noqa: E731
                                               f"<enable>{1 if on else 0}</enable>\n</{tag}>\n")
        return (row("ServerPort", "serverPort", bc_port, True) + row("HttpPort", "httpPort", http[1], http[0])
                + row("HttpsPort", "httpsPort", https[1], https[0]) + row("RtspPort", "rtspPort", rtsp[1], rtsp[0])
                + row("RtmpPort", "rtmpPort", 1935, False)
                + row("OnvifPort", "onvifPort", http[1], bool(onvif) and onvif.group(1) == "true"))

    async def set_server_ports(self, payload: bytes, bc_port: int) -> list[str]:
        """38: HTTPS and ONVIF follow the app. The proxy itself lives on Hikvision's HTTP
        and RTSP (and its own Baichuan port), so changes that would cut it off are refused."""
        root = _body(payload)
        doc = await self._accesses()

        def wanted(tag: str, port_tag: str) -> tuple[bool | None, int | None]:
            el = root.find(tag)
            if el is None:
                return None, None
            on, port = el.findtext("enable"), el.findtext(port_tag)
            return (on != "0") if on is not None else None, int(port) if port else None

        port = wanted("ServerPort", "serverPort")[1]
        if port not in (None, bc_port):
            raise ValueError(f"the proxy listens on {bc_port}: change listen in config.yaml")
        for tag, port_tag, proto in (("HttpPort", "httpPort", "HTTP"), ("RtspPort", "rtspPort", "RTSP")):
            on, port = wanted(tag, port_tag)
            cur_on, cur_port = self._access(doc, proto)
            if on is False and cur_on:
                raise ValueError(f"refusing to disable Hikvision {proto}: the proxy needs it")
            if port not in (None, cur_port):
                raise ValueError(f"refusing to move Hikvision {proto} to {port}: update config.yaml and the camera together")
        changed: list[str] = []
        on, port = wanted("HttpsPort", "httpsPort")
        cur_on, cur_port = self._access(doc, "HTTPS")
        if (on is not None and on != cur_on) or (port not in (None, cur_port)):
            def edit(m: re.Match) -> str:
                block = m.group(0)
                if _tag(block, "protocol") != "HTTPS":
                    return block
                block = _set(block, "enabled", str(cur_on if on is None else on).lower())
                return _set(block, "portNo", port or cur_port)
            doc = re.sub(r"<AdminAccessProtocol(?:\s[^>]*)?>.*?</AdminAccessProtocol>", edit, doc, flags=re.S)
            status, body = await self.http.put_bytes("/ISAPI/Security/adminAccesses", doc.encode("utf-8"))
            _check(status, body, "PUT adminAccesses")
            changed.append("HTTPS")
        on = wanted("OnvifPort", "onvifPort")[0]
        integrate = (await self.http.get_bytes("/ISAPI/System/Network/Integrate")).decode("utf-8")
        m = re.search(r"(<ONVIF[^>]*>.*?<enable>)(\w+)", integrate, re.S)
        if on is not None and m and (m.group(2) == "true") != on:
            new = integrate[:m.start(2)] + str(on).lower() + integrate[m.end(2):]
            status, body = await self.http.put_bytes("/ISAPI/System/Network/Integrate", new.encode("utf-8"))
            _check(status, body, "PUT Integrate")
            changed.append("ONVIF")
        return changed

    # ------------------------------------------------------------------ light
    @property
    def _light_path(self) -> str:
        return f"/ISAPI/Image/channels/{self.channel}/supplementLight"

    async def light_mode(self) -> str:
        doc = (await self.http.get_bytes(self._light_path)).decode("utf-8")
        m = re.search(r"<supplementLightMode>(\w+)", doc)
        return m.group(1) if m else "close"

    async def set_light_mode(self, mode: str) -> None:
        doc = (await self.http.get_bytes(self._light_path)).decode("utf-8")
        status, body = await self.http.put_bytes(self._light_path,
                                                 _set(doc, "supplementLightMode", mode).encode("utf-8"))
        _check(status, body, f"PUT light {mode}")

    # -------------------------------------------------------------- recording
    @property
    def _track_path(self) -> str:
        return f"/ISAPI/ContentMgmt/record/tracks/{self.channel}01"

    async def track(self) -> str:
        return (await self.http.get_bytes(self._track_path)).decode("utf-8")

    async def record_xml(self, event_types: list[str]) -> str:
        return record_xml(await self.track(), event_types)

    async def set_schedule(self, enable: bool, tables: dict[str, str]) -> None:
        doc = replace_schedule(await self.track(), schedule_actions(tables))
        doc = _set(doc, "enableSchedule", str(enable).lower())
        status, body = await self.http.put_bytes(self._track_path, doc.encode("utf-8"))
        _check(status, body, "PUT record schedule")

    async def record_cfg_xml(self) -> str:
        doc = await self.track()
        get = lambda tag, d: (re.findall(rf"<{tag}>(\w+)", doc) or [d])[0]  # noqa: E731
        return (f'<RecordCfg version="1.1">\n<channelId>0</channelId>\n'
                f'<cycle>{1 if get("LoopEnable", "true") == "true" else 0}</cycle>\n'
                f'<recordDelayTime>{get("PostRecordTimeSeconds", "5")}</recordDelayTime>\n'
                f'<preRecordTime>{get("PreRecordTimeSeconds", "5")}</preRecordTime>\n'
                "<packageTime>5</packageTime>\n<cyclelist>\n<item>0</item>\n<item>1</item>\n</cyclelist>\n</RecordCfg>")

    async def set_record_cfg(self, cycle: int | None, delay: int | None, pre: int | None) -> None:
        doc = await self.track()
        if cycle is not None:
            doc = _set(doc, "LoopEnable", "true" if cycle else "false")
        if delay is not None:
            doc = _set(doc, "PostRecordTimeSeconds", _nearest(POST_RECORD, delay))
        if pre is not None:
            doc = _set(doc, "PreRecordTimeSeconds", _nearest(PRE_RECORD, pre))
        status, body = await self.http.put_bytes(self._track_path, doc.encode("utf-8"))
        _check(status, body, "PUT record config")
