"""YAML config -> CameraConfig objects."""
from __future__ import annotations

from dataclasses import dataclass, field

import yaml

from .camera import CameraConfig
from .events import AI_TYPES, SMART_TYPES, Holds
from .media import StreamInfo


@dataclass
class AppConfig:
    cameras: list[CameraConfig]
    templates_dir: str | None = None
    state_dir: str | None = "state"   # settings written by NVR/app (camera name, OSD, image ...)
    unknown_reply: int = 200
    log_level: str = "info"
    trace: bool = False
    listen_host: str = "0.0.0.0"
    listen_port: int | None = None    # Hikvision HTTP push receiver
    discovery: tuple[str, int] | None = ("0.0.0.0", 2000)   # Reolink LAN search responder
    extra: dict = field(default_factory=dict)


def _hostport(s: str, default_port: int) -> tuple[str, int]:
    if ":" in s:
        h, p = s.rsplit(":", 1)
        return h or "0.0.0.0", int(p)
    return s, default_port


def _stream_info(d: dict | None) -> StreamInfo | None:
    if not d:
        return None
    return StreamInfo(codec=str(d.get("codec", "H264")).upper().replace("HEVC", "H265"),
                      width=int(d["width"]), height=int(d["height"]), fps=int(d.get("fps", 25)))


def load(path: str) -> AppConfig:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    cams = []
    for c in raw.get("cameras", []):
        hik = c.get("hik", {})
        ev = c.get("events", {})
        host, port = _hostport(str(c.get("listen", "0.0.0.0:9000")), 9000)
        ai_types = ev.get("ai_types", ["people", "vehicle"])
        bad = set(ai_types) - set(AI_TYPES)
        if bad:
            raise ValueError(f"{c['name']}: unknown ai_types {bad}, allowed {AI_TYPES}")
        smart = {k: v for k, v in (ev.get("smart") or {}).items() if v}
        bad = set(smart) - set(SMART_TYPES)
        if bad:
            raise ValueError(f"{c['name']}: unknown smart types {bad}, allowed {SMART_TYPES}")
        hold = ev.get("hold", {})
        cams.append(CameraConfig(
            name=c["name"], listen_host=host, listen_port=port,
            username=c.get("username", "admin"), password=str(c.get("password", "")),
            encryption=c.get("encryption", "full_aes"),
            nego_class=int(str(c.get("nego_class", "0x6614")), 0),
            display_name=c.get("display_name"),
            sync_name_to_hik=bool(hik.get("sync_name", True)),
            perf_msg_ids=tuple(int(x) for x in (c.get("performance") or {}).get("msg_ids", [])),
            perf_probe=bool((c.get("performance") or {}).get("probe", False)),
            **{k: str(c[k]) for k in ("model", "hardware", "firmware", "build_day", "detail", "serial", "uid", "mac")
               if k in c},
            hik_host=hik.get("host", ""), hik_port=int(hik.get("port", 80)),
            hik_user=hik.get("username", "admin"), hik_password=str(hik.get("password", "")),
            hik_channel=hik.get("channel", 1),
            main_rtsp=hik.get("main_rtsp", ""), sub_rtsp=hik.get("sub_rtsp", ""),
            snapshot_path=hik.get("snapshot_path", "/ISAPI/Streaming/channels/101/picture"),
            event_source=hik.get("events", "alertstream"),
            playback=bool(hik.get("playback", True)), playback_track=int(hik.get("playback_track", 101)),
            audio=bool(hik.get("audio", True)), light_seconds=int(hik.get("light_seconds", 30)),
            ai_types=ai_types, smart_zones=smart,
            holds=Holds(motion=float(hold.get("motion", 3)), ai=float(hold.get("ai", 5)),
                        smart=float(hold.get("smart", 5))),
            ai_implies_motion=bool(ev.get("ai_implies_motion", True)),
            untargeted_smart_target=ev.get("untargeted_smart_target"),
            event_map=ev.get("event_map") or {}, target_map=ev.get("target_map") or {},
            region_base=int(ev.get("region_base", 1)),
            main_info=_stream_info(hik.get("main_info")), sub_info=_stream_info(hik.get("sub_info")),
        ))
    lh, lp = (None, None)
    if raw.get("hik_listen"):
        lh, lp = _hostport(str(raw["hik_listen"]), 8088)
    disc = raw.get("discovery", True)
    discovery = None if disc is False else _hostport("0.0.0.0" if disc is True else str(disc), 2000)
    unknown = raw.get("unknown_reply", "ok")
    return AppConfig(
        cameras=cams, templates_dir=raw.get("templates_dir"),
        state_dir=raw.get("state_dir", "state") or None,
        unknown_reply=200 if unknown in ("ok", 200) else 400,
        log_level=raw.get("log_level", "info"), trace=bool(raw.get("trace", False)),
        listen_host=lh or "0.0.0.0", listen_port=lp, discovery=discovery,
    )
