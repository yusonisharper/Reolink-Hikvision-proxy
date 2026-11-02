"""RTSP (Hikvision) -> Annex-B access units, shared between NVR viewers."""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import time
from dataclasses import dataclass

from .bcmedia import AccessUnit, AnnexBParser

log = logging.getLogger("media")


@dataclass
class StreamInfo:
    codec: str = "H264"   # H264 / H265
    width: int = 1920
    height: int = 1080
    fps: int = 25


def _input_opts(url: str, probe: bool = False) -> list[str]:
    if url.startswith("rtsp"):
        return ["-rtsp_transport", "tcp"]
    # local file (testing): loop it in real time
    return [] if probe else ["-re", "-stream_loop", "-1"]


async def probe(url: str, ffprobe: str = "ffprobe", timeout: float = 15) -> StreamInfo:
    proc = await asyncio.create_subprocess_exec(
        ffprobe, "-v", "error", *_input_opts(url, probe=True), "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
        "-of", "json", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise
    s = json.loads(out or b"{}").get("streams", [{}])[0]
    codec = {"h264": "H264", "hevc": "H265", "h265": "H265"}.get(s.get("codec_name", ""), "")
    if not codec:
        raise RuntimeError(f"ffprobe {url}: unsupported/unknown codec: {err.decode(errors='replace')[-300:]}")
    fps = 25
    for k in ("avg_frame_rate", "r_frame_rate"):
        try:
            n, d = s.get(k, "0/0").split("/")
            if int(d) and 0 < int(n) / int(d) <= 120:
                fps = round(int(n) / int(d))
                break
        except (ValueError, ZeroDivisionError):
            pass
    return StreamInfo(codec, int(s.get("width") or 0), int(s.get("height") or 0), fps)


class RateMeter:
    """Bytes/s over a sliding window -> kbit/s."""

    def __init__(self, window: float = 5.0) -> None:
        self.window = window
        self.samples: collections.deque[tuple[float, int]] = collections.deque()

    def add(self, n: int) -> None:
        now = time.monotonic()
        self.samples.append((now, n))
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()

    def kbps(self) -> int | None:
        now = time.monotonic()
        while self.samples and now - self.samples[0][0] > self.window:
            self.samples.popleft()
        if not self.samples:
            return None
        span = max(now - self.samples[0][0], 1.0)
        return int(sum(n for _, n in self.samples) * 8 / 1000 / span)


class StreamSource:
    """One ffmpeg process per RTSP URL, started on first subscriber."""

    def __init__(self, name: str, url: str, info: StreamInfo | None = None,
                 ffmpeg: str = "ffmpeg", ffprobe: str = "ffprobe", linger: float = 10.0) -> None:
        self.name, self.url = name, url
        self.info = info
        self.ffmpeg, self.ffprobe = ffmpeg, ffprobe
        self.linger = linger
        self.subs: set[asyncio.Queue] = set()
        self._task: asyncio.Task | None = None
        self._stop_handle: asyncio.TimerHandle | None = None
        self.last_key: AccessUnit | None = None
        self.rate = RateMeter()

    async def ensure_info(self) -> StreamInfo:
        if self.info is None:
            self.info = await probe(self.url, self.ffprobe)
            log.info("%s: %s %dx%d@%d", self.name, self.info.codec, self.info.width,
                     self.info.height, self.info.fps)
        return self.info

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=300)
        self.subs.add(q)
        if self._stop_handle:
            self._stop_handle.cancel()
            self._stop_handle = None
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name=f"ffmpeg-{self.name}")
        elif self.last_key is not None:
            q.put_nowait((time.monotonic(), self.last_key))   # instant picture for late joiners
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subs.discard(q)
        if not self.subs and self._task and not self._stop_handle:
            self._stop_handle = asyncio.get_running_loop().call_later(self.linger, self._stop)

    def _stop(self) -> None:
        self._stop_handle = None
        if not self.subs and self._task:
            self._task.cancel()
            self._task = None
            self.last_key = None

    async def _run(self) -> None:
        info = await self.ensure_info()
        fmt = "h264" if info.codec == "H264" else "hevc"
        while self.subs:
            proc = await asyncio.create_subprocess_exec(
                self.ffmpeg, "-hide_banner", "-loglevel", "error", *_input_opts(self.url),
                "-i", self.url, "-map", "0:v:0", "-c:v", "copy", "-f", fmt, "pipe:1",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            parser = AnnexBParser(info.codec)
            log.info("%s: ffmpeg started (pid %s)", self.name, proc.pid)
            try:
                assert proc.stdout
                while True:
                    data = await proc.stdout.read(65536)
                    if not data:
                        break
                    self.rate.add(len(data))
                    for au in parser.feed(data):
                        if au.keyframe:
                            self.last_key = au
                        item = (time.monotonic(), au)
                        for q in list(self.subs):
                            if q.full():
                                # viewer too slow: drop its backlog, resume at next keyframe
                                while not q.empty():
                                    q.get_nowait()
                                q.put_nowait(None)
                            else:
                                q.put_nowait(item)
            except asyncio.CancelledError:
                proc.kill()
                raise
            finally:
                if proc.returncode is None:
                    proc.kill()
                    await proc.wait()
            err = (await proc.stderr.read()).decode(errors="replace")[-400:] if proc.stderr else ""
            log.warning("%s: ffmpeg exited (%s) %s", self.name, proc.returncode, err.strip())
            await asyncio.sleep(2)
