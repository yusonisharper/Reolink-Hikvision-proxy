"""A virtual Reolink camera backed by a Hikvision camera."""
from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

from . import hik
from .bc import body_xml
from .events import AI_TYPES, SMART_TYPES, DetEvent, EventState, Holds, Snapshot
from .media import RateMeter, StreamInfo, StreamSource
from .settings import SettingsStore
from .templates import Templates

log = logging.getLogger("camera")

AITYPE_BITS = {"people": 1 << 1, "vehicle": 1 << 2, "face": 1 << 3, "dog_cat": 1 << 4}
SMART_BITS = {"crossline": 1 << 1, "intrusion": 1 << 2, "loitering": 1 << 3}


@dataclass
class CameraConfig:
    name: str
    listen_host: str = "0.0.0.0"
    listen_port: int = 9000
    username: str = "admin"
    password: str = ""
    encryption: str = "full_aes"       # max we accept: none | bc | aes | full_aes
    nego_class: int = 0x6614           # header class of the nonce reply (0x6614 or 0x0000)
    display_name: str | None = None    # initial name shown in NVR/app (default: name)
    sync_name_to_hik: bool = True      # rename from NVR/app also renames the Hikvision channel (OSD)
    perf_msg_ids: tuple = ()           # msg ids answered with <Performance> (experimental, see README)
    perf_probe: bool = False           # answer each id with marker values to find the right one
    model: str = "RLC-811A"
    hardware: str = "IPC_523128M8MP"
    firmware: str = "v3.1.0.2368_23062700"
    build_day: str = "23062700"
    detail: str = "IPC_523128M8MP0000000000000"
    serial: str = ""
    uid: str = ""
    # Hikvision
    hik_host: str = ""
    hik_port: int = 80
    hik_user: str = "admin"
    hik_password: str = ""
    hik_channel: int | None = 1
    main_rtsp: str = ""
    sub_rtsp: str = ""
    snapshot_path: str = "/ISAPI/Streaming/channels/101/picture"
    event_source: str = "alertstream"  # alertstream | listen | none
    # events
    ai_types: list[str] = field(default_factory=lambda: ["people", "vehicle"])
    smart_zones: dict[str, list[dict]] = field(default_factory=dict)
    holds: Holds = field(default_factory=Holds)
    ai_implies_motion: bool = True
    untargeted_smart_target: str | None = None
    event_map: dict[str, str] = field(default_factory=dict)
    target_map: dict[str, str] = field(default_factory=dict)
    region_base: int = 1
    # stream overrides (skip ffprobe)
    main_info: StreamInfo | None = None
    sub_info: StreamInfo | None = None


# Reolink-style option lists; the current value is always merged in.
FPS_TABLE = [30, 25, 22, 20, 18, 16, 15, 12, 10, 8, 6, 4, 2]
MAIN_KBPS = [1024, 1536, 2048, 3072, 4096, 5120, 6144, 7168, 8192]
SUB_KBPS = [64, 128, 160, 192, 256, 384, 512, 768, 1024]


def _hik_bitrate(doc: str) -> int:
    vbr = re.search(r"<videoQualityControlType>\s*vbr", doc, re.I)
    m = (re.search(r"<vbrUpperCap>(\d+)</vbrUpperCap>", doc) if vbr else None) or \
        re.search(r"<constantBitRate>(\d+)</constantBitRate>", doc) or \
        re.search(r"<vbrUpperCap>(\d+)</vbrUpperCap>", doc)
    return int(m.group(1)) if m else 0


def _table(values: list[int], cur: int, limit: int | None = None) -> str:
    vals = {v for v in values if limit is None or v <= limit} | {cur}
    return ",".join(str(v) for v in sorted(vals, reverse=values[0] > values[-1]))


class VirtualCamera:
    def __init__(self, cfg: CameraConfig, templates_dir: str | None = None,
                 unknown_reply: int = 200, state_dir: str | None = None) -> None:
        self.cfg = cfg
        self.templates = Templates(cfg.name, templates_dir)
        self.settings = SettingsStore(state_dir, cfg.name)
        self.unknown_reply = unknown_reply
        self.state = EventState(holds=cfg.holds, ai_implies_motion=cfg.ai_implies_motion)
        mapper_kw = {}
        if cfg.event_map:
            mapper_kw["event_map"] = {**hik.DEFAULT_EVENT_MAP, **{k.lower(): v for k, v in cfg.event_map.items()}}
        if cfg.target_map:
            mapper_kw["target_map"] = {**hik.DEFAULT_TARGET_MAP, **{k.lower(): v for k, v in cfg.target_map.items()}}
        self.mapper = hik.EventMapper(untargeted_smart_target=cfg.untargeted_smart_target,
                                      channel=cfg.hik_channel, region_base=cfg.region_base, **mapper_kw)
        self.http = hik.HikHttp(cfg.hik_host, cfg.hik_port, cfg.hik_user, cfg.hik_password) if cfg.hik_host else None
        self.main = StreamSource(f"{cfg.name}/main", cfg.main_rtsp, cfg.main_info) if cfg.main_rtsp else None
        self.sub = StreamSource(f"{cfg.name}/sub", cfg.sub_rtsp or cfg.main_rtsp, cfg.sub_info) \
            if (cfg.sub_rtsp or cfg.main_rtsp) else None
        self.alarm_subs: set = set()        # BcSession objects
        self.last_snapshot = Snapshot()
        self.uid = cfg.uid or ("9527" + os.urandom(6).hex().upper())
        self.serial = cfg.serial or os.urandom(7).hex().upper()
        self._tasks: list[asyncio.Task] = []
        self.tx = RateMeter()               # bytes sent to NVR/app (all sessions)
        self.hik_cpu: int | None = None
        self.hik_bitrate: int | None = None  # configured main-stream bitrate (kbps), fallback
        self.hik_sub_bitrate: int | None = None

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        for src in (self.main, self.sub):
            if src and src.info is None:
                try:
                    await src.ensure_info()
                except Exception as e:  # noqa: BLE001 - keep running, NVR may still connect
                    log.warning("%s: probe %s failed: %s", self.cfg.name, src.url, e)
        if self.cfg.event_source == "alertstream" and self.http:
            self._tasks.append(asyncio.create_task(hik.alert_stream(self.http, self.on_alert)))
        self._tasks.append(asyncio.create_task(self._ticker()))
        if self.http:
            self._tasks.append(asyncio.create_task(self._poll_status()))

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()

    # ---------------------------------------------------------------- events
    def on_alert(self, alert: hik.HikAlert) -> None:
        evs = self.mapper.map(alert)
        if alert.event_type.lower() != "videoloss":   # videoloss/inactive is Hik's heartbeat
            log.debug("%s: hik %s %s ch=%s regions=%s -> %s", self.cfg.name, alert.event_type,
                      alert.state, alert.channel, alert.regions, evs)
        for ev in evs:
            # drop target types the virtual camera does not advertise
            targets = frozenset(t for t in ev.targets if t in self.cfg.ai_types)
            kind = ev.kind
            if kind in SMART_TYPES and not self.cfg.smart_zones.get(kind):
                # smart type not advertised: still report the person/vehicle as AI + motion
                kind = "motion"
            self.state.apply(DetEvent(kind, ev.active, ev.zone, targets))
        if evs:
            self._maybe_push()

    async def _poll_status(self, interval: float = 10) -> None:
        """CPU usage + configured bitrate from the Hikvision camera (for NVR "Performance")."""
        warned = False
        while True:
            try:
                st = (await self.http.get_bytes("/ISAPI/System/status")).decode("utf-8", "replace")
                m = re.search(r"<cpuUtilization>(\d+)</cpuUtilization>", st)
                self.hik_cpu = int(m.group(1)) if m else None
                if self.hik_bitrate is None:
                    for idx, attr in ((1, "hik_bitrate"), (2, "hik_sub_bitrate")):
                        sc = (await self.http.get_bytes(
                            f"/ISAPI/Streaming/channels/{self.cfg.hik_channel}0{idx}")).decode("utf-8", "replace")
                        setattr(self, attr, _hik_bitrate(sc))
                    log.info("%s: Hikvision bitrate main=%s sub=%s kbps", self.cfg.name,
                             self.hik_bitrate, self.hik_sub_bitrate)
            except Exception as e:  # noqa: BLE001
                if not warned:
                    log.warning("%s: Hikvision status poll failed: %s", self.cfg.name, e)
                    warned = True
            await asyncio.sleep(interval)

    def performance_xml(self, msg_id: int) -> bytes:
        if self.cfg.perf_probe:     # e.g. 112 -> CPU 12 %, 1112 kbps
            cpu, rate, net = msg_id % 100, 1000 + msg_id, 1000 + msg_id
        else:
            main = self.main.rate.kbps() if self.main else None
            rate = main if main is not None else (self.hik_bitrate or 0)
            cpu = self.hik_cpu if self.hik_cpu is not None else 0
            net = self.tx.kbps() or 0
        return body_xml(f'<Performance version="1.1">\n<channelId>0</channelId>\n'
                        f"<cpuUsed>{cpu}</cpuUsed>\n<netThroughput>{net}</netThroughput>\n"
                        f"<codecRate>{rate}</codecRate>\n</Performance>")

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            self._maybe_push()

    def _maybe_push(self) -> None:
        snap = self.state.snapshot()
        if snap == self.last_snapshot:
            return
        self.last_snapshot = snap
        log.info("%s: state motion=%s ai=%s smart=%s", self.cfg.name, snap.motion,
                 ",".join(sorted(snap.ai)) or "-", snap.smart or "-")
        for s in list(self.alarm_subs):
            s.push_alarm(snap)

    # -------------------------------------------------------------- template
    @property
    def aitype_bits(self) -> int:
        return sum(AITYPE_BITS[t] for t in self.cfg.ai_types if t in AITYPE_BITS)

    @property
    def smartai_bits(self) -> int:
        return sum(SMART_BITS[t] for t in SMART_TYPES if self.cfg.smart_zones.get(t))

    @property
    def display_name(self) -> str:
        return self.settings.name or self.cfg.display_name or self.cfg.name

    def rename(self, new: str) -> bool:
        """Called when the NVR/app writes a new camera name. Returns True if changed."""
        new = new.strip()
        if not new or new == self.display_name:
            return False
        old = self.display_name
        self.settings.set_name(new)
        log.info("%s: renamed %r -> %r", self.cfg.name, old, new)
        if self.cfg.sync_name_to_hik and self.http:
            self._tasks.append(asyncio.create_task(self._sync_name(new)))
        return True

    async def _sync_name(self, name: str) -> None:
        path = f"/ISAPI/System/Video/inputs/channels/{self.cfg.hik_channel}"
        try:
            doc = (await self.http.get_bytes(path)).decode("utf-8")
            new_doc, n = re.subn(r"<name>.*?</name>", f"<name>{xml_escape(name)}</name>", doc, count=1,
                                 flags=re.S)
            if not n:
                raise ValueError("no <name> in VideoInputChannel")
            status, body = await self.http.put_bytes(path, new_doc.encode("utf-8"))
            if status != 200:
                raise ConnectionError(f"HTTP {status}: {body[:200]!r}")
            log.info("%s: Hikvision channel name set to %r", self.cfg.name, name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: could not rename Hikvision channel: %s", self.cfg.name, e)

    def template_vars(self, username: str = "") -> dict:
        t = time.localtime()
        mi = (self.main.info if self.main and self.main.info else None) or StreamInfo()
        si = (self.sub.info if self.sub and self.sub.info else None) or StreamInfo(width=640, height=360, fps=15)
        return {
            "name": xml_escape(self.display_name), "model": self.cfg.model, "hardware": self.cfg.hardware,
            "firmware": self.cfg.firmware, "build_day": self.cfg.build_day, "detail": self.cfg.detail,
            "serial": self.serial, "uid": self.uid, "username": username or self.cfg.username,
            "main_w": mi.width, "main_h": mi.height, "main_fps": mi.fps,
            "sub_w": si.width, "sub_h": si.height, "sub_fps": si.fps,
            "main_venc": 1 if mi.codec == "H265" else 0, "sub_venc": 1 if si.codec == "H265" else 0,
            "main_kbps": self.hik_bitrate or 4096, "sub_kbps": self.hik_sub_bitrate or 512,
            "main_fps_table": _table(FPS_TABLE, mi.fps, max(mi.fps, 2)),
            "sub_fps_table": _table(FPS_TABLE, si.fps, max(si.fps, 2)),
            "main_kbps_table": _table(MAIN_KBPS, self.hik_bitrate or 4096),
            "sub_kbps_table": _table(SUB_KBPS, self.hik_sub_bitrate or 512),
            "year": t.tm_year, "month": t.tm_mon, "day": t.tm_mday,
            "hour": t.tm_hour, "minute": t.tm_min, "second": t.tm_sec,
            "tz_offset": -t.tm_gmtoff,
            "aitype_bits": self.aitype_bits, "smartai_bits": self.smartai_bits,
            "detect_types": ",".join(x for x in AI_TYPES if x in self.cfg.ai_types) or "none",
        }

    async def snapshot_jpeg(self) -> bytes | None:
        if not self.http:
            return None
        try:
            return await self.http.get_bytes(self.cfg.snapshot_path)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: snapshot failed: %s", self.cfg.name, e)
            return None
