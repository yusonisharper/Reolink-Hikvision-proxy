"""A virtual Reolink camera backed by a Hikvision or Dahua camera.

The hik_* names below predate Dahua support and hold whichever camera is configured:
hikcfg is a HikConfig or DahuaConfig, recordings a HikRecordings or DahuaRecordings.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import time
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

from . import dahua, discovery, hik
from . import settings as st
from .bc import body_xml, xml_value
from .events import AI_TYPES, SMART_TYPES, DetEvent, EventState, Holds, Snapshot
from .hikcfg import HikConfig
from .media import AudioSource, RateMeter, StreamInfo, StreamSource
from .playback import EventJournal, HikRecordings
from .settings import SettingsStore
from .templates import Templates

log = logging.getLogger("camera")

FRAME_RATE_MODE_KEY = 25      # settings store slot (write id of the image page)
AITYPE_BITS = {"people": 1 << 1, "vehicle": 1 << 2, "face": 1 << 3, "dog_cat": 1 << 4}
AITYPE_TAMPER = 1 << 25       # the app's Tamper Alert (reolink_aio: aitype bit 25)
# The app's Motion filter: libBCSDKWrapper's GetSupportMDEvent tests bit 0 of the field where GetSupportPeopleEvent tests bit 1.
AITYPE_MOTION = 1 << 0
AITYPE_TAMPER_EVENT = 1 << 13   # notifications: GetSupportTamperEvent. Bit 25 is only the settings switch.
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
    model: str = "RLC-811A"
    hardware: str = "IPC_NT7NA58MP"
    firmware: str = "v3.0.0.4308_2506201230"
    build_day: str = "2506201230"
    detail: str = "IPC_NT7NA58MPS35E1W01100000001"
    serial: str = ""
    uid: str = ""                      # P2P UID; empty (default) = IP-only, like a camera with P2P off
    mac: str = ""                      # MAC in LAN search replies (default: derived from name)
    # source camera
    vendor: str = "hikvision"          # hikvision | dahua
    hik_host: str = ""
    hik_port: int = 80
    hik_user: str = "admin"
    hik_password: str = ""
    hik_channel: int | None = 1
    main_rtsp: str = ""
    sub_rtsp: str = ""
    snapshot_path: str = "/ISAPI/Streaming/channels/101/picture"
    event_source: str = "alertstream"  # hikvision: alertstream | listen | none; dahua: attach | none
    playback: bool = True              # serve the Hikvision SD card recordings to the app/NVR
    playback_track: int = 101          # Hikvision record track (101 = main stream)
    audio: bool = True                 # forward the microphone (main stream audio, AAC transcoded)
    light_seconds: int = 30            # the manual light switches itself off after this long
    ptz_max_seconds: int = 30          # a PTZ move without a stop ends after this long
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


# 151 <AbilityInfo> block that makes the app offer playback (from a real camera)
RECORD_ABILITY = ("<record>\n<subModule>\n<channelId>0</channelId>\n"
                  "<abilityValue>download_rw, fileFind_rw, recordCfg_rw, recordSchedule_rw, subStreamRecord_ro"
                  "</abilityValue>\n"
                  "</subModule>\n</record>\n")
# Dahua: playback only, the record settings are not connected to the camera
RECORD_ABILITY_PLAYBACK = RECORD_ABILITY.replace("recordCfg_rw, recordSchedule_rw, ", "")
PTZ_STOP_TRIES = 3
ZOOM_SPEED = 1                # the speed argument hardly changes how fast a zoom goes
ZOOM_RATE = 4500              # ZoomMapValue per second, measured on a real camera
ZOOM_TRIES = 4
ZOOM_CLOSE_ENOUGH = 300       # no move for a smaller difference
ZOOM_STALLED = 50             # a pulse that moved less than this hit the end of the lens
PTZ_ABILITY = "<PTZ>\n<subModule>\n<channelId>0</channelId>\n<abilityValue>control_rw, preset_rw{}</abilityValue>\n" \
              "</subModule>\n</PTZ>\n"

# Reolink-style option lists; the current value is always merged in.
FPS_TABLE = [30, 25, 22, 20, 18, 16, 15, 12, 10, 8, 6, 4, 2]
MAIN_KBPS = [1024, 1536, 2048, 3072, 4096, 5120, 6144, 7168, 8192]
SUB_KBPS = [64, 128, 160, 192, 256, 384, 512, 768, 1024]


def _table(values: list[int], cur: int, limit: int | None = None) -> str:
    vals = {v for v in values if limit is None or v <= limit} | {cur}
    return ",".join(str(v) for v in sorted(vals, reverse=values[0] > values[-1]))


def _encode_table(stype: str, venc: int, size: tuple[int, int], fps: int, kbps: int,
                  fps_table: str, kbps_table: str, gop: int) -> str:
    return (f"<encodeTable>\n<type>{stype}</type>\n<videoEncType>{venc}</videoEncType>\n"
            f"<resolution>\n<width>{size[0]}</width>\n<height>{size[1]}</height>\n</resolution>\n"
            f"<defaultFramerate>{fps}</defaultFramerate>\n<defaultBitrate>{kbps}</defaultBitrate>\n"
            f"<framerateTable>{fps_table}</framerateTable>\n<bitrateTable>{kbps_table}</bitrateTable>\n"
            f"<defaultGop>{gop}</defaultGop>\n</encodeTable>\n")


class VirtualCamera:
    def __init__(self, cfg: CameraConfig, templates_dir: str | None = None,
                 unknown_reply: int = 200, state_dir: str | None = None) -> None:
        self.cfg = cfg
        self.templates = Templates(cfg.name, templates_dir)
        self.settings = SettingsStore(state_dir, cfg.name)
        self.unknown_reply = unknown_reply
        self.state = EventState(holds=cfg.holds, ai_implies_motion=cfg.ai_implies_motion)
        is_dahua = cfg.vendor == "dahua"
        self.vendor_label = "Dahua" if is_dahua else "Hikvision"
        event_map = dahua.DEFAULT_EVENT_MAP if is_dahua else hik.DEFAULT_EVENT_MAP
        target_map = dahua.DEFAULT_TARGET_MAP if is_dahua else hik.DEFAULT_TARGET_MAP
        self.mapper = hik.EventMapper(event_map={**event_map, **{k.lower(): v for k, v in cfg.event_map.items()}},
                                      target_map={**target_map, **{k.lower(): v for k, v in cfg.target_map.items()}},
                                      untargeted_smart_target=cfg.untargeted_smart_target,
                                      channel=cfg.hik_channel, region_base=cfg.region_base)
        http_type, config_type = (dahua.DahuaHttp, dahua.DahuaConfig) if is_dahua else (hik.HikHttp, HikConfig)
        self.http = http_type(cfg.hik_host, cfg.hik_port, cfg.hik_user, cfg.hik_password) if cfg.hik_host else None
        self.hikcfg = config_type(self.http, cfg.hik_channel) if self.http else None
        if self.hikcfg and not is_dahua:
            self.hikcfg.scene_is_tamper = self.mapper.event_map.get("scenechangedetection") == "tamper"
        self.audio = AudioSource(f"{cfg.name}/audio", cfg.main_rtsp) if cfg.audio and cfg.main_rtsp else None
        self.audio_on = False               # Hikvision main stream carries audio
        self.light_status = False
        self._light_restore = "close"
        self._light_task: asyncio.Task | None = None
        self._ptz_lock = asyncio.Lock()
        self._ptz_code: str | None = None   # what the camera is doing until a stop
        self._ptz_owner: object | None = None
        self._ptz_watchdog: asyncio.Task | None = None
        self._zoom_task: asyncio.Task | None = None
        self.ptz_tour: int | None = None    # the patrol started from here, for a stop that names none
        self.main =StreamSource(f"{cfg.name}/main", cfg.main_rtsp, cfg.main_info) if cfg.main_rtsp else None
        self.sub = StreamSource(f"{cfg.name}/sub", cfg.sub_rtsp or cfg.main_rtsp, cfg.sub_info) \
            if (cfg.sub_rtsp or cfg.main_rtsp) else None
        self.recordings = None
        if self.http and cfg.playback and cfg.main_rtsp:
            self.recordings = dahua.DahuaRecordings(self.http, cfg.hik_channel) if is_dahua \
                else HikRecordings(self.http, cfg.playback_track)
        # detections with time, to label playback files people/vehicle/...
        self.journal = EventJournal(self.settings.dir / "events.jsonl" if self.settings.dir else None)
        self.sessions: set = set()          # all logged-in BcSessions (msg 120 online users)
        self.last_snapshot = Snapshot()
        self.uid = cfg.uid
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
                    log.warning("%s: probe of %s failed: %s", self.cfg.name, src.name, e or type(e).__name__)
        if self.hikcfg and not await self._load_hik():
            # camera rebooting or unreachable: don't lose playback, light and sizes until our restart
            log.warning("%s: %s not reachable, will keep trying", self.cfg.name, self.vendor_label)
            self._tasks.append(asyncio.create_task(self._retry_load_hik()))
        events = {"alertstream": hik.alert_stream, "attach": dahua.event_stream}.get(self.cfg.event_source)
        if events and self.http:
            self._tasks.append(asyncio.create_task(events(self.http, self.on_alert)))
        self._tasks.append(asyncio.create_task(self._ticker()))
        if self.http:
            self._tasks.append(asyncio.create_task(self._poll_status()))

    async def _load_hik(self) -> bool:
        """What the source camera offers. False when it could not be asked."""
        try:
            if self.recordings:
                disk = await self.recordings.storage()
                if disk is None:
                    log.info("%s: playback off (no SD card / HDD)", self.cfg.name)
                    self.recordings = None
                else:
                    log.info("%s: playback from %s storage (%d of %d MB free)", self.cfg.name,
                             self.vendor_label, disk[1], disk[0])
            await self.hikcfg.load()
            self.audio_on = bool(self.audio) and await self.hikcfg.audio_enabled()
            if self.hikcfg.light_on_mode:
                self.light_status = await self.hikcfg.light_mode() == self.hikcfg.light_on_mode
            await self._use_camera_frame_rates()
        except (OSError, asyncio.TimeoutError) as e:
            log.debug("%s: %s capabilities: %s", self.cfg.name, self.vendor_label, e)
            return False
        except Exception as e:  # noqa: BLE001 - keep running without these features
            log.warning("%s: %s capabilities: %s", self.cfg.name, self.vendor_label, e)
        log.info("%s: streams %s, audio %s, light %s", self.cfg.name,
                 {k: v.sizes for k, v in self.hikcfg.caps.items()}, "on" if self.audio_on else "off",
                 self.hikcfg.light_on_mode or "none")
        return True

    async def _use_camera_frame_rates(self) -> None:
        """What ffprobe reads from the stream can be wrong (60 for a Dahua stream at 30), and the frame
        rate goes to the NVR in every preview."""
        for idx, src in ((1, self.main), (2, self.sub)):
            if src and src.info:
                fps = (await self.hikcfg.stream_state(idx))[3]
                if fps > 0 and fps != src.info.fps:
                    log.info("%s: %s runs at %d fps, the stream says %d", self.cfg.name, src.name, fps, src.info.fps)
                    src.info = dataclasses.replace(src.info, fps=fps)

    async def _retry_load_hik(self, interval: float = 30) -> None:
        while not await self._load_hik():
            await asyncio.sleep(interval)

    def failed_tasks(self) -> list[asyncio.Task]:
        """Background tasks that ended with an exception. They stay referenced here, so asyncio
        never reports them by itself."""
        return [t for t in self._tasks if t.done() and not t.cancelled() and t.exception()]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        await self._ptz_stop_logged()
        if self._light_task:      # don't leave a timed light burning without its timer
            try:
                await self.set_light(False)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: could not switch the light off: %s", self.cfg.name, e)

    # ------------------------------------------------------------------ light
    async def set_light(self, on: bool) -> None:
        """Manual light from the preview page; goes back to its previous mode by itself."""
        if not self.hikcfg or not self.hikcfg.light_on_mode:
            raise ValueError("no light on this camera")
        task, self._light_task = self._light_task, None
        if task and task is not asyncio.current_task():
            task.cancel()
        if on:
            mode = await self.hikcfg.light_mode()
            if mode != self.hikcfg.light_on_mode:
                self._light_restore = mode
                await self.hikcfg.set_light_mode(self.hikcfg.light_on_mode)
            self._light_task = asyncio.create_task(self._light_timer())
        else:
            await self.hikcfg.set_light_mode(self._light_restore)
        log.info("%s: light %s", self.cfg.name, f"on for {self.cfg.light_seconds} s" if on else "off")
        self.light_status = on
        for s in list(self.sessions):
            s.push_light(on)

    async def _light_timer(self) -> None:
        await asyncio.sleep(self.cfg.light_seconds)
        try:
            await self.set_light(False)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: could not switch the light off: %s", self.cfg.name, e)

    # -------------------------------------------------------------------- PTZ
    async def ptz_start(self, session: object, code: str, speed: int) -> None:
        """Moves until ptz_stop. The app is expected to send stop on release; if it does not,
        or its connection goes, the watchdog and ptz_release stop the camera anyway."""
        if self._zoom_task and self._zoom_task is not asyncio.current_task():
            self._zoom_task.cancel()  # this move replaces a zoom to a position
            self._zoom_task = None
        async with self._ptz_lock:
            if self._ptz_code and self._ptz_code != code:
                await self._ptz_halt()
            self._ptz_code, self._ptz_owner = code, session
            try:
                await self.hikcfg.ptz_start(code, speed)
            except Exception:
                self._ptz_code = self._ptz_owner = None
                raise
            if self._ptz_watchdog:
                self._ptz_watchdog.cancel()
            self._ptz_watchdog = asyncio.create_task(self._ptz_timeout())

    async def ptz_stop(self) -> None:
        async with self._ptz_lock:
            await self._ptz_halt()

    async def ptz_tour_start(self, tour: int) -> None:
        """A tour runs on the camera by itself: no watchdog, and it outlives the app's connection."""
        async with self._ptz_lock:
            await self._ptz_halt()
            await self.hikcfg.ptz_tour_start(tour)
            self.ptz_tour = tour

    async def ptz_tour_stop(self, tour: int | None) -> None:
        async with self._ptz_lock:
            await self.hikcfg.ptz_tour_stop(tour or self.ptz_tour or 1)
            self.ptz_tour = None

    async def ptz_goto(self, preset: int) -> None:
        async with self._ptz_lock:
            await self._ptz_halt()
            await self.hikcfg.ptz_goto(preset)

    async def ptz_zoom_to(self, session: object, target: int) -> None:
        """Start zooming to a position and return; the app can send other commands meanwhile,
        and any stop or other move takes over."""
        if self._zoom_task:
            self._zoom_task.cancel()
        await self.ptz_stop()
        self._zoom_task = asyncio.create_task(self._zoom_follow(session, max(0, min(dahua.ZOOM_MAX, target))))

    async def _zoom_follow(self, session: object, target: int) -> None:
        """Timed pulses, each corrected from where the last one ended. Stopping when the position
        is seen to arrive overshoots by far: a status request can take a second, at ZOOM_RATE."""
        try:
            for _ in range(ZOOM_TRIES):
                cur = (await self.hikcfg.zoom_focus())[0]
                if abs(target - cur) <= ZOOM_CLOSE_ENOUGH:
                    return
                code = "ZoomTele" if target > cur else "ZoomWide"
                await self.ptz_start(session, code, ZOOM_SPEED)
                await asyncio.sleep(abs(target - cur) / ZOOM_RATE)
                if self._ptz_code != code:
                    return                  # a stop or another move came in
                await self.ptz_stop()
                if abs((await self.hikcfg.zoom_focus())[0] - cur) < ZOOM_STALLED:
                    return                  # did not move: the end of the lens
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("%s: zoom to %d failed: %s", self.cfg.name, target, e)
            await self._ptz_stop_logged()

    async def _ptz_halt(self) -> None:
        """A camera left moving is worse than a slow answer, so a failed stop is tried again."""
        code = self._ptz_code
        if code is None:
            return
        watchdog, self._ptz_watchdog = self._ptz_watchdog, None
        if watchdog and watchdog is not asyncio.current_task():
            watchdog.cancel()
        for attempt in range(PTZ_STOP_TRIES):
            try:
                await self.hikcfg.ptz_stop(code)
                self._ptz_code = self._ptz_owner = None
                return
            except (OSError, asyncio.TimeoutError) as e:
                log.warning("%s: PTZ stop failed (try %d): %s", self.cfg.name, attempt + 1, e)
                if attempt + 1 == PTZ_STOP_TRIES:
                    raise
                await asyncio.sleep(1)

    async def _ptz_timeout(self) -> None:
        await asyncio.sleep(self.cfg.ptz_max_seconds)
        log.warning("%s: PTZ still moving after %d s without a stop: stopping", self.cfg.name, self.cfg.ptz_max_seconds)
        await self._ptz_stop_logged()

    async def _ptz_stop_logged(self) -> None:
        try:
            await self.ptz_stop()
        except Exception as e:  # noqa: BLE001
            log.error("%s: could not stop PTZ: %s", self.cfg.name, e)

    def ptz_release(self, session: object) -> None:
        """A connection ended: nobody is left to send its stop."""
        if self._ptz_owner is session and self._ptz_code:
            log.info("%s: stopping PTZ, its client disconnected", self.cfg.name)
            self._tasks.append(asyncio.create_task(self._ptz_stop_logged()))

    # ---------------------------------------------------------------- streams
    def stream_info_list(self) -> str:
        """146: one <StreamInfo> per main size, like real cameras: the sub stream size is
        fixed (the Reolink app does not offer changing it), so only the current one is listed."""
        mi = (self.main.info if self.main and self.main.info else None) or StreamInfo()
        si = (self.sub.info if self.sub and self.sub.info else None) or StreamInfo(width=640, height=360, fps=15)
        caps = self.hikcfg.caps if self.hikcfg else {}
        tables = []
        for idx, stype, info, kbps, rates in ((1, "mainStream", mi, self.hik_bitrate or 4096, MAIN_KBPS),
                                              (2, "subStream", si, self.hik_sub_bitrate or 512, SUB_KBPS)):
            c = caps.get(idx)
            sizes = sorted(set(c.sizes if c and idx == 1 else []) | {(info.width, info.height)}, reverse=True)
            venc = 1 if info.codec == "H265" else 0
            fps_t = _table(FPS_TABLE, info.fps, c.max_fps if c else max(info.fps, 2))
            kbps_t = _table(rates, kbps, c.max_kbps if c else None)
            gop = 2 if idx == 1 else 4
            tables.append([_encode_table(stype, venc, s, info.fps, kbps, fps_t, kbps_t, gop) for s in sizes])
        subs = "".join(tables[1])
        infos = "".join(f"<StreamInfo>\n<channelBits>1</channelBits>\n{m}{subs}</StreamInfo>\n" for m in tables[0])
        return f'<StreamInfoList version="1.1">\n{infos}</StreamInfoList>'

    @property
    def frame_rate_mode(self) -> int:
        """Image page "frame rate mode" (constantFrameRate). Hikvision has no such setting,
        so it is only remembered here; CBR/VBR is the stream page's bitrate mode."""
        doc = self.settings.get(FRAME_RATE_MODE_KEY)
        value = xml_value(doc, "constantFrameRate") if doc else None
        return int(value) if value else 0

    def set_frame_rate_mode(self, value: int) -> None:
        if value != self.frame_rate_mode:
            self.settings.put(FRAME_RATE_MODE_KEY, body_xml(f"<constantFrameRate>{value}</constantFrameRate>"))

    async def apply_compression(self, payload: bytes) -> None:
        """57: resolution / frame rate / bitrate / codec go to the source camera."""
        root = st._body(payload)
        for idx, tag, src, attr in ((1, "mainStream", self.main, "hik_bitrate"),
                                    (2, "subStream", self.sub, "hik_sub_bitrate")):
            el = root.find(f"Compression/{tag}")
            if el is None or src is None:
                continue
            info = src.info or StreamInfo()
            kbps_now = getattr(self, attr) or 0
            num = lambda t, d: int(el.findtext(t) or d)  # noqa: E731
            w, h, fps, kbps = num("width", info.width), num("height", info.height), \
                num("frame", info.fps), num("bitRate", kbps_now)
            # videoEncType and encoderType are ignored: the app sends 0 / vbr whatever it was told.
            # Bitrate mode comes with the image page (encType), see HikConfig.apply_image.
            audio = None
            if idx == 1 and self.audio and el.findtext("audio") is not None:
                audio = el.findtext("audio") != "0"
                if audio == self.audio_on:
                    audio = None
            video_changed = (w, h, fps) != (info.width, info.height, info.fps)
            if not video_changed and kbps == kbps_now and audio is None:
                continue
            await self.hikcfg.set_stream(idx, w, h, fps, kbps, audio)
            codec, w, h, fps, kbps = await self.hikcfg.stream_state(idx)
            log.info("%s: %s %s now %dx%d %d fps %d kbps %s %s", self.cfg.name, self.vendor_label, tag, w, h, fps,
                     kbps, codec, self.hikcfg.bitrate_mode.get(idx, "vbr"))
            setattr(self, attr, kbps)
            if audio is not None:
                self.audio_on = audio
            if video_changed:
                src.reconfigure(StreamInfo(codec, w, h, fps))

    async def request_keyframe(self, stream_type: str) -> None:
        if not self.hikcfg:
            return
        try:
            if self.hikcfg.can_request_keyframe:
                await self.hikcfg.request_keyframe(1 if stream_type == "mainStream" else 2)
            else:
                src = self.main if stream_type == "mainStream" else self.sub
                if src:
                    await src.request_keyframe()
        except (OSError, asyncio.TimeoutError, ValueError) as e:  # the next keyframe just comes by itself
            log.debug("%s: request keyframe: %s", self.cfg.name, e)

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
                # smart type not advertised: still report the person/vehicle as AI + motion. Its end says
                # nothing about motion, which may still be going on: what it set just runs out by itself.
                if not ev.active:
                    continue
                kind = "motion"
            self.state.apply(DetEvent(kind, ev.active, ev.zone, targets))
            if ev.active and (targets or kind in SMART_TYPES):
                self.journal.add(set(targets) | ({kind} if kind in SMART_TYPES else set()))
        if evs:
            self._maybe_push()

    async def _poll_status(self, interval: float = 10) -> None:
        """CPU usage + configured bitrate from the source camera (for NVR "Performance")."""
        warned = False
        while True:
            try:
                self.hik_cpu = await self.hikcfg.cpu_usage()
                if self.hik_bitrate is None:
                    self.hik_bitrate, self.hik_sub_bitrate = await self.hikcfg.bitrates()
                    log.info("%s: %s bitrate main=%s sub=%s kbps", self.cfg.name, self.vendor_label,
                             self.hik_bitrate, self.hik_sub_bitrate)
            except Exception as e:  # noqa: BLE001
                if not warned:
                    log.warning("%s: %s status poll failed: %s", self.cfg.name, self.vendor_label, e)
                    warned = True
            await asyncio.sleep(interval)

    def performance_xml(self) -> bytes:
        """122 reply; names from libBCSDKWrapper's PerformanceInfo parser. Bitrate as measured while somebody
        watches, otherwise as configured on the camera."""
        main = self.main.rate.kbps() if self.main else None
        rate = main if main is not None else (self.hik_bitrate or 0)
        cpu = self.hik_cpu if self.hik_cpu is not None else 0
        return body_xml(f'<PerformanceInfo version="1.1">\n<cpuUseRate>{cpu}</cpuUseRate>\n'
                        f"<codeRate>{rate}</codeRate>\n<netDataRate>{self.tx.kbps() or 0}</netDataRate>\n"
                        "</PerformanceInfo>")

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(0.25)
            self._maybe_push()

    def _maybe_push(self) -> None:
        snap = self.state.snapshot()
        if snap == self.last_snapshot:
            return
        self.last_snapshot = snap
        log.info("%s: state motion=%s tamper=%s ai=%s smart=%s", self.cfg.name, snap.motion, snap.tamper,
                 ",".join(sorted(snap.ai)) or "-", snap.smart or "-")
        for s in list(self.sessions):
            s.push_alarm(snap)

    # -------------------------------------------------------------- template
    @property
    def aitype_bits(self) -> int:
        bits = sum(AITYPE_BITS[t] for t in self.cfg.ai_types if t in AITYPE_BITS) | AITYPE_MOTION
        return bits | (AITYPE_TAMPER | AITYPE_TAMPER_EVENT if self.hikcfg and self.hikcfg.has_tamper else 0)

    @property
    def smartai_bits(self) -> int:
        return sum(SMART_BITS[t] for t in SMART_TYPES if self.cfg.smart_zones.get(t))

    @property
    def mac(self) -> str:
        return (self.cfg.mac or discovery.default_mac(self.cfg.name)).upper()

    @property
    def ip(self) -> str:
        h = self.cfg.listen_host
        return h if h not in ("", "0.0.0.0") else discovery.local_ip_for(self.cfg.hik_host or "8.8.8.8")

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
        try:
            await self.hikcfg.set_channel_name(name)
            log.info("%s: %s channel name set to %r", self.cfg.name, self.vendor_label, name)
        except Exception as e:  # noqa: BLE001
            log.warning("%s: could not rename %s channel: %s", self.cfg.name, self.vendor_label, e)

    def template_vars(self, username: str = "") -> dict:
        t = time.localtime()
        mi = (self.main.info if self.main and self.main.info else None) or StreamInfo()
        si = (self.sub.info if self.sub and self.sub.info else None) or StreamInfo(width=640, height=360, fps=15)
        ptz_type = self.hikcfg.ptz_type if self.hikcfg else 0
        return {
            "name": xml_escape(self.display_name), "model": self.cfg.model, "hardware": self.cfg.hardware,
            "firmware": self.cfg.firmware, "build_day": self.cfg.build_day, "detail": self.cfg.detail,
            "serial": self.serial, "uid": self.uid, "username": username or self.cfg.username,
            "uid_ability": "uid_ro, " if self.uid else "", "p2p_ability": "ptop_rw, " if self.uid else "",
            "stream_info_list": self.stream_info_list(), "audio_on": 1 if self.audio_on else 0,
            "main_mode": self.hikcfg.bitrate_mode.get(1, "vbr") if self.hikcfg else "vbr",
            "sub_mode": self.hikcfg.bitrate_mode.get(2, "vbr") if self.hikcfg else "vbr",
            "no_audio": 0 if self.audio else 1, "audio_version": 31 if self.audio else 0,
            "led_ctrl": 6 if self.hikcfg and self.hikcfg.light_on_mode else 0,
            "light_type": 1 if self.hikcfg and self.hikcfg.light_on_mode else 0,
            "light_seconds": self.cfg.light_seconds,
            "disk_num": 1 if self.recordings else 0, "record_ver": 3 if self.recordings else 0,
            "replay_version": 21 if self.recordings else 0,
            "record_ability": "" if not self.recordings
            else RECORD_ABILITY_PLAYBACK if self.cfg.vendor == "dahua" else RECORD_ABILITY,
            # reolink_aio confirms ptzType and ptzPreset; ptzMode, ptzControl and the PTZ ability are inferred
            "ptz_type": ptz_type, "ptz_mode": "pt" if ptz_type == 2 else "none", "ptz_on": 1 if ptz_type else 0,
            "ptz_preset": 1 if self.hikcfg and self.hikcfg.has_presets else 0,
            "ptz_patrol": 1 if self.hikcfg and self.hikcfg.has_tours else 0,
            "ptz_ability": "" if not ptz_type
            else PTZ_ABILITY.format(", cruise_rw" if self.hikcfg.has_tours else ""),
            "bc_port": self.cfg.listen_port, "ip": self.ip, "mask": "255.255.255.0",
            "gateway": self.ip.rsplit(".", 1)[0] + ".1", "mac": self.mac.lower(),
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
