"""Playback: Reolink record search / replay, backed by the Hikvision SD card.

Reolink app flow (captured from a real camera):

  102  HddInfoList            storage size
  142  DayRecords             which days of a month have recordings
  14   FileInfoList (open)    search a day -> <handle>
  15   FileInfoList (handle)  the files of that search
  16   FileInfoList (handle)  close
  381  ReplayByTimeV2         play from a time: 200, 200 + <replayStreamInfo>,
                              then BcMedia frames whose header carries <seq>
                              in the response/class bytes
  382  -                      stop replay
  383  ReplaySeekV2           jump inside a replay: new <seq> + <seekTime>; frames continue on
                              the 381 request with the new seq (not captured from a real camera)

Replay plays the one selected recording and stops; the app's list merges clips (message size
limit), so a seek into such a gap lands on the next recording.
  298  CoverPreview           timeline thumbnails (not supported: 400)

Hikvision side: ISAPI dailyDistribution + ContentMgmt/search, and RTSP
/Streaming/tracks/<track>?starttime=...  The app speaks camera-local wall
clock; Hikvision search and RTSP times ending in "Z" are real UTC, so they
are converted with the camera's current UTC offset (/ISAPI/System/time).
"""
from __future__ import annotations

import bisect
import calendar
import datetime as dt
import json
import logging
import re
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .hik import HikHttp, _strip_ns

log = logging.getLogger("playback")

MSG_FILE_OPEN, MSG_FILE_LIST, MSG_FILE_CLOSE = 14, 15, 16
MSG_HDD, MSG_DAY_RECORDS, MSG_COVER = 102, 142, 298
MSG_REPLAY, MSG_REPLAY_STOP, MSG_REPLAY_SEEK = 381, 382, 383
MAX_REPLY_BYTES = 36000
LIST_CACHE_SECONDS = 30

# Hikvision recordType metadata -> Reolink recordType
HIK_RECORD_TYPES = {"timing": "sched", "motion": "md", "manual": "manual", "alarm": "io",
                    "motionoralarm": "md", "motionandalarm": "md", "allevent": "md",
                    "smart": "md", "linedetection": "md", "fielddetection": "md"}


@dataclass
class Segment:
    start: dt.datetime
    end: dt.datetime
    rec_type: str = "sched"


# Reolink recordType of a file; the most specific one wins when segments merge
TYPE_RANK = ["people", "vehicle", "dog_cat", "face", "crossline", "intrusion", "loitering", "md", "io",
             "manual", "sched"]


def join_types(*types: str) -> str:
    """Union of recordType values, most specific first. "people,intrusion,md" lets a clip match
    every app filter, motion too: a person is motion as well. sched only stays when alone."""
    parts = {p for t in types for p in t.split(",") if p}
    rank = lambda t: TYPE_RANK.index(t) if t in TYPE_RANK else len(TYPE_RANK)  # noqa: E731
    events = sorted(parts - {"sched"}, key=rank)
    return ",".join(events) if events else "sched"


class EventJournal:
    """What the camera detected, with time: Hikvision's record index only says
    "motionOrAlarm", so recordings are labelled people/vehicle/... from the
    live alerts the proxy received (one JSON line per detection)."""

    def __init__(self, path: Path | None, keep_days: int = 30) -> None:
        self.path, self.keep = path, dt.timedelta(days=keep_days)
        self.items: list[tuple[dt.datetime, str]] = []
        if path and path.is_file():
            cutoff = dt.datetime.now() - self.keep
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    d = json.loads(line)
                    t = dt.datetime.fromisoformat(d["t"])
                except (ValueError, KeyError, TypeError):
                    continue
                if t >= cutoff:
                    self.items.extend((t, k) for k in d.get("k", []))
            self._rewrite()

    def _rewrite(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lines = (json.dumps({"t": t.isoformat(timespec="seconds"), "k": [k]}) for t, k in self.items)
        self.path.write_text("".join(x + "\n" for x in lines), encoding="utf-8")

    def add(self, types: set[str], when: dt.datetime | None = None) -> None:
        when = (when or dt.datetime.now()).replace(microsecond=0)
        recent = {k for t, k in self.items[-20:] if (when - t).total_seconds() < 5}
        new = sorted(set(types) - recent)    # Hikvision repeats an active alert every second
        if not new:
            return
        self.items.extend((when, k) for k in new)
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps({"t": when.isoformat(timespec="seconds"), "k": new}) + "\n")

    def label(self, seg: Segment, slack: float = 5) -> str:
        lo = seg.start - dt.timedelta(seconds=slack)
        hi = seg.end + dt.timedelta(seconds=slack)
        i = bisect.bisect_left(self.items, (lo, ""))
        found = set()
        while i < len(self.items) and self.items[i][0] <= hi:
            found.add(self.items[i][1])
            i += 1
        return join_types(seg.rec_type, *found)


# ------------------------------------------------------------------ XML time
def parse_time(el: ET.Element | None) -> dt.datetime | None:
    """<startTime><year>..</year>...</startTime> -> naive local datetime."""
    if el is None:
        return None
    try:
        v = {k: int(el.findtext(k) or 0) for k in ("year", "month", "day", "hour", "minute", "second")}
        return dt.datetime(**v)
    except ValueError:
        return None


def time_xml(tag: str, t: dt.datetime, year_offset: int = 0) -> str:
    return (f"<{tag}>\n<year>{t.year - year_offset}</year>\n<month>{t.month}</month>\n<day>{t.day}</day>\n"
            f"<hour>{t.hour}</hour>\n<minute>{t.minute}</minute>\n<second>{t.second}</second>\n</{tag}>\n")


def wall_seconds(t: dt.datetime) -> int:
    """Reolink's frame time: local wall clock encoded as if it were UTC."""
    return calendar.timegm(t.timetuple())


def body_root(payload: bytes, tag: str) -> ET.Element | None:
    try:
        root = ET.fromstring(payload)
    except ET.ParseError:
        return None
    return root if root.tag == tag else root.find(f".//{tag}")


# --------------------------------------------------------------- Hikvision
def _hik_time(utc: dt.datetime) -> str:
    return utc.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_offset(s: str) -> dt.timedelta | None:
    """Offset suffix of an ISO time: "Z" -> 0, "-08:00" -> -8h, none -> None."""
    s = s.strip()
    if s.endswith("Z"):
        return dt.timedelta(0)
    m = re.search(r"([+-])(\d\d):?(\d\d)$", s[19:])
    if not m:
        return None
    d = dt.timedelta(hours=int(m.group(2)), minutes=int(m.group(3)))
    return -d if m.group(1) == "-" else d


def _parse_hik_time(s: str, cam_offset: dt.timedelta) -> dt.datetime:
    """Hikvision time -> camera-local wall clock."""
    wall = dt.datetime.strptime(s.strip()[:19], "%Y-%m-%dT%H:%M:%S")
    off = parse_offset(s)
    return wall if off is None else wall - off + cam_offset


class HikRecordings:
    def __init__(self, http: HikHttp, track: int = 101) -> None:
        self.http, self.track = http, track
        self._offset: tuple[float, dt.timedelta] | None = None   # (monotonic, UTC offset)
        self._lists: dict[tuple, tuple[float, list[Segment]]] = {}    # full searches, see search()

    async def utc_offset(self) -> dt.timedelta:
        """Camera's current UTC offset (DST aware, refreshed every 10 minutes)."""
        now = time.monotonic()
        if self._offset is None or now - self._offset[0] > 600:
            root = ET.fromstring(await self.http.get_bytes("/ISAPI/System/time"))
            _strip_ns(root)
            self._offset = (now, parse_offset(root.findtext("localTime") or "") or dt.timedelta(0))
        return self._offset[1]

    async def storage(self) -> tuple[int, int] | None:
        """(capacity MB, free MB) of the first disk."""
        root = ET.fromstring(await self.http.get_bytes("/ISAPI/ContentMgmt/Storage"))
        _strip_ns(root)
        hdd = root.find(".//hdd")
        if hdd is None:
            return None
        return int(hdd.findtext("capacity") or 0), int(hdd.findtext("freeSpace") or 0)

    async def record_days(self, year: int, month: int) -> set[int]:
        body = (f'<?xml version="1.0" encoding="utf-8"?><trackDailyParam><year>{year}</year>'
                f"<monthOfYear>{month}</monthOfYear></trackDailyParam>").encode()
        status, data = await self.http.post_bytes(
            f"/ISAPI/ContentMgmt/record/tracks/{self.track}/dailyDistribution", body)
        if status != 200:
            raise ConnectionError(f"dailyDistribution -> HTTP {status}")
        root = ET.fromstring(data)
        _strip_ns(root)
        return {int(d.findtext("dayOfMonth") or 0) for d in root.iter("day")
                if (d.findtext("record") or "").strip().lower() == "true"}

    async def search(self, start: dt.datetime, end: dt.datetime, limit: int = 1000) -> list[Segment]:
        """Recordings overlapping [start, end] (camera-local), in local time, oldest first.
        Full lists take several requests and the app asks again on every visit: kept for a moment."""
        key = (start, end, limit)
        cached = self._lists.get(key)
        if limit > 1 and cached and time.monotonic() - cached[0] < LIST_CACHE_SECONDS:
            return cached[1]
        out: list[Segment] = []
        sid = str(uuid.uuid4())
        off = await self.utc_offset()
        while len(out) < limit:
            body = (f'<?xml version="1.0" encoding="utf-8"?><CMSearchDescription><searchID>{sid}</searchID>'
                    f"<trackList><trackID>{self.track}</trackID></trackList>"
                    f"<timeSpanList><timeSpan><startTime>{_hik_time(start - off)}</startTime>"
                    f"<endTime>{_hik_time(end - off)}</endTime></timeSpan></timeSpanList>"
                    f"<maxResults>{min(100, limit - len(out))}</maxResults>"
                    f"<searchResultPostion>{len(out)}</searchResultPostion>"
                    "<metadataList><metadataDescriptor>//recordType.meta.std-cgi.com</metadataDescriptor>"
                    "</metadataList></CMSearchDescription>").encode()
            status, data = await self.http.post_bytes("/ISAPI/ContentMgmt/search", body)
            if status != 200:
                raise ConnectionError(f"ContentMgmt/search -> HTTP {status}")
            root = ET.fromstring(data)
            _strip_ns(root)
            items = list(root.iter("searchMatchItem"))
            for it in items:
                s, e = it.findtext("timeSpan/startTime"), it.findtext("timeSpan/endTime")
                if not s or not e:
                    continue
                meta = (it.findtext(".//metadataDescriptor") or "").rsplit("/", 1)[-1].lower()
                out.append(Segment(_parse_hik_time(s, off), _parse_hik_time(e, off),
                                   HIK_RECORD_TYPES.get(meta, "sched")))
            if (root.findtext("responseStatusStrg") or "").upper() != "MORE" or not items:
                break
        if limit > 1:
            self._lists = {key: (time.monotonic(), out)}
        return out

    async def replay_url(self, live_rtsp: str, start: dt.datetime, end: dt.datetime) -> str:
        """rtsp://user:pass@host:554/Streaming/Channels/101 -> .../Streaming/tracks/<track>?starttime=...
        (start/end camera-local; the URL takes UTC)."""
        off = await self.utc_offset()
        return self.make_replay_url(live_rtsp, self.track, start - off, end - off)

    @staticmethod
    def make_replay_url(live_rtsp: str, track: int, start_utc: dt.datetime, end_utc: dt.datetime) -> str:
        u = urlsplit(live_rtsp)
        q = f"starttime={start_utc:%Y%m%dT%H%M%SZ}&endtime={end_utc:%Y%m%dT%H%M%SZ}"
        return urlunsplit((u.scheme, u.netloc, f"/Streaming/tracks/{track}", q, ""))


# ------------------------------------------------------------ Reolink XML
def hdd_info_xml(capacity_mb: int, free_mb: int) -> str:
    return ('<HddInfoList version="1.1">\n<HddInfo>\n<number>0</number>\n'
            f"<capacity>{capacity_mb // 1024}</capacity>\n<capacityM>{capacity_mb % 1024}</capacityM>\n"
            "<format>1</format>\n<mount>1</mount>\n"
            f"<remainSize>{free_mb // 1024}</remainSize>\n<remainSizeM>{free_mb % 1024}</remainSizeM>\n"
            "</HddInfo>\n</HddInfoList>")


def day_records_xml(start: dt.datetime, end: dt.datetime, days: list[dt.date]) -> str:
    types = "".join(f"<dayType>\n<index>{(d - start.date()).days}</index>\n<type>normal</type>\n</dayType>\n"
                    for d in sorted(days))
    return ('<DayRecords version="1.1">\n' + time_xml("startTime", start) + time_xml("endTime", end)
            + "<DayRecordList>\n<DayRecord>\n<index>0</index>\n<channelId>0</channelId>\n"
            + (f"<dayTypeList>\n{types}</dayTypeList>\n" if types else "")
            + "</DayRecord>\n</DayRecordList>\n</DayRecords>")


def file_open_xml(handle: int, start: dt.datetime, end: dt.datetime) -> str:
    return ('<FileInfoList version="1.1">\n<FileInfo>\n<channelId>0</channelId>\n'
            f"<handle>{handle}</handle>\n<name></name>\n<containsAudio>0</containsAudio>\n"
            "<fileType>h264</fileType>\n<recordType>manual,io,md,sched,people,vehicle,face,other,dog_cat,"
            "crossline,intrusion,loitering</recordType>\n"
            "<supportSub>1</supportSub>\n<sizeL>0</sizeL>\n<sizeH>0</sizeH>\n"
            + time_xml("startTime", start) + time_xml("endTime", end) + "</FileInfo>\n</FileInfoList>")


def file_list_xml(handle: int, stream: str, segs: list[Segment]) -> str:
    items = "".join(
        f"<FileInfo>\n<channelId>0</channelId>\n<handle>{handle}</handle>\n"
        f"<name>01{s.start:%Y%m%d%H%M%S}</name>\n<bdst>0</bdst>\n"
        f"<Id>/hik/{s.start:%Y-%m-%d}/Rec_{s.start:%Y%m%d_%H%M%S}_{s.end:%H%M%S}.mp4</Id>\n"
        f"<streamType>{stream}</streamType>\n<containsAudio>0</containsAudio>\n<fileType>mp4</fileType>\n"
        f"<recordType>{s.rec_type}</recordType>\n<sizeL>0</sizeL>\n<sizeH>0</sizeH>\n<supportSub>1</supportSub>\n"
        + time_xml("startTime", s.start) + time_xml("endTime", s.end) + "</FileInfo>\n"
        for s in segs)
    return f'<FileInfoList version="1.1">\n{items}</FileInfoList>'


def replay_info_xml(seq: int, stream: str, width: int, height: int, fps: int,
                    begin: dt.datetime, end: dt.datetime) -> str:
    # years since 1900 here, unlike everywhere else (as sent by real cameras)
    return ("<replayStreamInfo>\n<streamHeader>\n<ver>1002</ver>\n"
            f"<width>{width}</width>\n<height>{height}</height>\n<streamType>{stream}</streamType>\n"
            f"<frameRate>{fps}</frameRate>\n" + time_xml("beginTime", begin, 1900)
            + time_xml("endTime", end, 1900) + "</streamHeader>\n"
            f"<frm_tm_sec>{wall_seconds(begin)}</frm_tm_sec>\n<play_tm_ms>0</play_tm_ms>\n"
            f"<seq>{seq}</seq>\n</replayStreamInfo>")


def merge_segments(segs: list[Segment], start: dt.datetime, end: dt.datetime,
                   max_items: int = 100) -> list[Segment]:
    """Clip to [start, end] and join close segments until at most max_items remain.

    Hikvision motion recording yields hundreds of short clips a day, far more
    than real Reolink cameras list. Clips of the same type are joined first, so
    the app's type filters keep working; different types only as a last resort.
    Every recorded second stays covered.
    """
    segs = sorted((Segment(max(s.start, start), min(s.end, end), s.rec_type)
                   for s in segs if s.end > start and s.start < end), key=lambda s: s.start)
    gaps = (1, 10, 30, 60, 120, 300, 600, 1800, 3600, 86400)
    out: list[Segment] = []
    for any_type, gap in [(False, g) for g in gaps] + [(True, g) for g in gaps]:
        out = []
        for s in segs:
            near = out and (s.start - out[-1].end).total_seconds() <= gap
            if near and (any_type or s.rec_type == out[-1].rec_type):
                last = out[-1]
                out[-1] = Segment(last.start, max(last.end, s.end), join_types(last.rec_type, s.rec_type))
            else:
                out.append(s)
        if len(out) <= max_items:
            break
    return out


def file_list_limited(handle: int, stream: str, segs: list[Segment], start: dt.datetime,
                      end: dt.datetime) -> tuple[str, int]:
    """15 reply: (xml, entries). The app hangs on a reply over ~40 KB (real cameras never send
    a message above 39488 bytes), so the list is merged further until it fits with room to spare."""
    for cap in (60, 50, 40, 30, 20, 10):
        merged = merge_segments(segs, start, end, max_items=cap)
        xml = file_list_xml(handle, stream, merged)
        if len(xml.encode()) <= MAX_REPLY_BYTES:
            break
    return xml, len(merged)


def months_between(start: dt.date, end: dt.date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def seq_of(payload: bytes) -> int:
    m = re.search(rb"<seq>(\d+)</seq>", payload)
    return int(m.group(1)) if m else 0
