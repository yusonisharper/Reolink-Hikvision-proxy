"""Log files and the systemd watchdog."""
import asyncio
import logging
import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bcproxy import logsetup, watchdog

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def logs(tmp_path):
    root = logging.getLogger()
    level, hook = root.level, sys.excepthook
    handlers = logsetup.setup("info", str(tmp_path / "logs"))
    yield tmp_path / "logs"
    import faulthandler
    faulthandler.disable()
    for h in handlers:
        root.removeHandler(h)
        h.close()
    root.setLevel(level)
    sys.excepthook = hook


def read(path):
    for h in logging.getLogger().handlers:
        h.flush()
    return path.read_text(encoding="utf-8")


def test_levels_go_to_the_right_files(logs):
    log = logging.getLogger("t")
    log.info("just info")
    log.warning("a warning")
    try:
        raise KeyError("boom")
    except KeyError:
        log.exception("an error")
    everything, errors = read(logs / "bcproxy.log"), read(logs / "errors.log")
    assert "just info" in everything and "a warning" in everything
    assert "just info" not in errors
    assert "a warning" in errors and "an error" in errors
    assert "KeyError" in errors      # the traceback is what makes it debuggable


@pytest.mark.parametrize("text, secret", [
    ("ffmpeg rtsp://admin:hunter2@192.168.0.23:554/Streaming/Channels/101 failed", "hunter2"),
    ("http://u:hunter2@cam/x", "hunter2"),
    ("GET /a?user=admin&password=hunter2&x=1", "hunter2"),
    ("<User><password>hunter2</password></User>", "hunter2"),
])
def test_credentials_are_not_written(logs, text, secret):
    logging.getLogger("t").error(text)
    logging.getLogger("t").error("in a traceback", exc_info=ValueError(text))
    content = read(logs / "errors.log")
    assert "***" in content and secret not in content


def test_redact_keeps_what_is_useful():
    out = logsetup.redact("rtsp://admin:pw@192.168.0.23:554/Streaming/Channels/101 timed out")
    assert out == "rtsp://***@192.168.0.23:554/Streaming/Channels/101 timed out"
    assert logsetup.redact("user@example.com said a://b") == "user@example.com said a://b"


def test_unwritable_log_dir_does_not_stop_the_proxy(tmp_path, caplog):
    blocker = tmp_path / "file"
    blocker.write_text("")
    root = logging.getLogger()
    level, hook = root.level, sys.excepthook
    handlers = logsetup.setup("info", str(blocker / "logs"))
    try:
        assert len(handlers) == 1
        assert "cannot write logs" in caplog.text
    finally:
        for h in handlers:
            root.removeHandler(h)
        root.setLevel(level)
        sys.excepthook = hook


def test_broken_config_is_logged_to_file(tmp_path):
    bad = tmp_path / "config.yaml"
    bad.write_text("cameras: [")
    r = subprocess.run([sys.executable, "-m", "bcproxy", "-c", str(bad), "--log-dir", str(tmp_path / "l")],
                       cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode == 2
    assert "cannot load" in (tmp_path / "l" / "errors.log").read_text(encoding="utf-8")


def test_interval_is_half_of_what_systemd_allows(monkeypatch):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)
    assert watchdog.interval() is None
    monkeypatch.setenv("WATCHDOG_USEC", "60000000")
    assert watchdog.interval() == 30
    monkeypatch.setenv("WATCHDOG_USEC", "nonsense")
    assert watchdog.interval() is None


def test_notify_without_systemd(monkeypatch):
    monkeypatch.delenv("NOTIFY_SOCKET", raising=False)
    assert watchdog.notify("READY=1") is False


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="needs unix datagram sockets")
def test_notify_reaches_the_socket(monkeypatch, tmp_path):
    path = str(tmp_path / "n")
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
        s.bind(path)
        monkeypatch.setenv("NOTIFY_SOCKET", path)
        assert watchdog.notify("READY=1") is True
        assert s.recv(64) == b"READY=1"


def fake_cam(task):
    return SimpleNamespace(cfg=SimpleNamespace(name="cam"), failed_tasks=lambda: [task] if task.done() else [])


async def _dead_task():
    async def die():
        raise KeyError("no such field")
    task = asyncio.create_task(die(), name="events")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    return task


def test_dead_task_asks_for_a_restart_under_systemd(monkeypatch, caplog):
    monkeypatch.setenv("WATCHDOG_USEC", "100000")
    beats = []
    monkeypatch.setattr(watchdog, "notify", beats.append)

    async def run():
        return await asyncio.wait_for(watchdog.supervise([fake_cam(await _dead_task())], asyncio.Event()), 5)

    assert asyncio.run(run()) is True
    assert beats == []       # no heartbeat for a broken service
    assert "background task events died" in caplog.text and "KeyError" in caplog.text


def test_dead_task_is_only_logged_without_a_supervisor(monkeypatch, caplog):
    monkeypatch.delenv("WATCHDOG_USEC", raising=False)

    async def run():
        stop = asyncio.Event()
        running = asyncio.create_task(watchdog.supervise([fake_cam(await _dead_task())], stop, period=0.01))
        await asyncio.sleep(0.1)
        stop.set()
        return await running

    assert asyncio.run(run()) is False
    assert caplog.text.count("background task events died") == 1


def test_heartbeats_while_healthy_until_stopped(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "20000")
    beats = []
    monkeypatch.setattr(watchdog, "notify", beats.append)

    async def run():
        stop = asyncio.Event()
        running = asyncio.create_task(watchdog.supervise([], stop))
        await asyncio.sleep(0.1)
        stop.set()
        return await running

    assert asyncio.run(run()) is False
    assert len(beats) >= 2 and set(beats) == {"WATCHDOG=1"}


def test_real_camera_reports_its_failed_tasks():
    from bcproxy.camera import VirtualCamera

    async def run():
        async def die():
            raise KeyError("x")

        async def fine():
            return None

        async def forever():
            await asyncio.sleep(60)

        cam = VirtualCamera.__new__(VirtualCamera)
        bad, good, live = asyncio.create_task(die()), asyncio.create_task(fine()), asyncio.create_task(forever())
        cancelled = asyncio.create_task(forever())
        cam._tasks = [bad, good, live, cancelled]
        await asyncio.sleep(0)
        cancelled.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert cam.failed_tasks() == [bad]
        live.cancel()

    asyncio.run(run())


def test_ffconcat_quotes_the_url():
    from bcproxy.media import _ffconcat
    assert _ffconcat("rtsp://u:it's@h/1", {"rtsp_transport": "tcp"}) == (
        "ffconcat version 1.0\nfile 'rtsp://u:it'\\''s@h/1'\noption rtsp_transport tcp\n")


class _Spawned:
    def __init__(self, monkeypatch):
        self.calls = []

        async def fake(*args, **kwargs):
            self.calls.append((args, kwargs))
            fds = kwargs.get("pass_fds", ())
            self.content = os.pread(fds[0], 4096, 0).decode() if fds else None
            return SimpleNamespace()
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


@pytest.mark.skipif(not hasattr(os, "memfd_create"), reason="needs memfd")
def test_password_stays_off_the_command_line(monkeypatch):
    from bcproxy.media import _spawn
    spawned = _Spawned(monkeypatch)
    url = "rtsp://admin:hunter2@192.168.0.23:554/Streaming/Channels/101"
    asyncio.run(_spawn("ffmpeg", ["-hide_banner"], url, ["pipe:1"], rtsp={"allowed_media_types": "audio"}))
    args, kwargs = spawned.calls[0]
    assert not any("hunter2" in a for a in args)
    assert args[args.index("-i") + 1] == f"/proc/self/fd/{kwargs['pass_fds'][0]}"
    assert url in spawned.content and "option allowed_media_types audio" in spawned.content
    with pytest.raises(OSError):       # the parent's copy is closed
        os.fstat(kwargs["pass_fds"][0])


def test_without_memfd_the_url_is_passed_directly(monkeypatch):
    from bcproxy import media
    spawned = _Spawned(monkeypatch)
    monkeypatch.setattr(media, "_secret_fd", lambda url, options: None)
    url = "rtsp://admin:hunter2@h/1"
    asyncio.run(media._spawn("ffmpeg", [], url, ["pipe:1"], rtsp={"allowed_media_types": "audio"}))
    assert spawned.calls[0][0] == ("ffmpeg", "-rtsp_transport", "tcp", "-allowed_media_types", "audio", "-i", url, "pipe:1")


def test_local_file_keeps_its_options(monkeypatch):
    from bcproxy.media import _spawn
    spawned = _Spawned(monkeypatch)
    asyncio.run(_spawn("ffmpeg", [], "a@b.mp4", ["pipe:1"], file_args=("-re",)))
    assert spawned.calls[0][0] == ("ffmpeg", "-re", "-i", "a@b.mp4", "pipe:1")


def _fake_ip(monkeypatch, present, fail_add=False):
    from bcproxy import netalias
    calls = []

    async def fake(*args):
        calls.append(args)
        if args[:2] == ("-4", "-o"):
            return 0, "\n".join(f"3: eth0    inet {h}/24 brd 192.168.0.255 scope global eth0" for h in present)
        return (1, "RTNETLINK answers: Operation not permitted") if fail_add else (0, "")
    monkeypatch.setattr(netalias, "_ip", fake)
    return netalias, calls


def test_only_missing_addresses_are_added(monkeypatch):
    netalias, calls = _fake_ip(monkeypatch, ["192.168.0.9", "192.168.0.51"])
    added = asyncio.run(netalias.ensure("eth0", ["192.168.0.51", "192.168.0.54"]))
    assert added == ["192.168.0.54"]
    assert calls[1:] == [("addr", "add", "192.168.0.54/32", "dev", "eth0")]


def test_an_address_is_not_mistaken_for_a_longer_one(monkeypatch):
    netalias, _ = _fake_ip(monkeypatch, ["192.168.0.510"])      # not an address, but shows the match is exact
    assert asyncio.run(netalias.ensure("eth0", ["192.168.0.51"])) == ["192.168.0.51"]


def test_failing_to_add_is_logged_and_does_not_raise(monkeypatch, caplog):
    netalias, _ = _fake_ip(monkeypatch, [], fail_add=True)
    assert asyncio.run(netalias.ensure("eth0", ["192.168.0.51"])) == []
    assert "cannot add 192.168.0.51 to eth0" in caplog.text


def test_lost_address_is_reported_as_a_warning(monkeypatch, caplog):
    netalias, _ = _fake_ip(monkeypatch, [])
    asyncio.run(netalias.ensure("eth0", ["192.168.0.51"]))
    assert any(r.levelname == "WARNING" and "192.168.0.51" in r.getMessage() for r in caplog.records)


def test_listen_addresses_skip_the_wildcard_and_duplicates():
    from bcproxy.netalias import listen_addresses
    cams = [SimpleNamespace(listen_host=h) for h in ("192.168.0.52", "0.0.0.0", "", "192.168.0.51", "192.168.0.52")]
    assert listen_addresses(cams) == ["192.168.0.51", "192.168.0.52"]


def test_config_reads_alias_interface(tmp_path):
    from bcproxy.config import load
    f = tmp_path / "c.yaml"
    f.write_text("alias_interface: eth0\ncameras: []\n")
    assert load(str(f)).alias_interface == "eth0"
    f.write_text("cameras: []\n")
    assert load(str(f)).alias_interface is None


def test_supervise_runs_the_check_every_round(monkeypatch):
    monkeypatch.setenv("WATCHDOG_USEC", "20000")
    monkeypatch.setattr(watchdog, "notify", lambda s: True)
    rounds = []

    async def run():
        stop = asyncio.Event()

        async def check():
            rounds.append(1)
        running = asyncio.create_task(watchdog.supervise([], stop, check=check))
        await asyncio.sleep(0.1)
        stop.set()
        await running

    asyncio.run(run())
    assert len(rounds) >= 2


def test_audio_never_lands_inside_a_video_frame():
    """Seen on a real NVR as a garbled picture: AAC between the chunks of an I frame broke the whole GOP."""
    from bcproxy.bcmedia import AccessUnit
    from bcproxy.media import StreamInfo
    from bcproxy.session import MEDIA_CHUNK, BcSession

    async def run():
        video, audio = asyncio.Queue(), asyncio.Queue()
        for i in range(4):
            video.put_nowait((i / 15, AccessUnit(b"\x00\x00\x00\x01\x26" + bytes(4 * MEDIA_CHUNK + 100), True)))
        for _ in range(200):
            audio.put_nowait(b"\xff\xf1" + bytes(60))
        src = SimpleNamespace(ensure_info=lambda: _info(), subscribe=lambda **kw: video, unsubscribe=lambda q: None, info=None)
        s = BcSession.__new__(BcSession)
        sent = []

        async def _info():
            return src.info

        async def send(msg):
            sent.append(msg)
            await asyncio.sleep(0)      # what a busy socket does in drain()
        src.info = StreamInfo("H265", 640, 360, 15)
        s.send, s.tag, s._spawn = send, "t", asyncio.create_task
        s.cam = SimpleNamespace(hikcfg=None, audio_on=True, audio=SimpleNamespace(subscribe=lambda: audio, unsubscribe=lambda q: None))
        req = SimpleNamespace(reply=lambda code, payload=b"", **kw: payload)
        task = asyncio.create_task(s._stream(req, src, "mainStream"))
        while video.qsize() or len(sent) < 30:
            await asyncio.sleep(0)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return sent[1:]

    from bcproxy.bcmedia import MAGIC_AAC, MAGIC_IFRAME
    import struct
    is_audio = lambda p: p[:4] == struct.pack("<I", MAGIC_AAC)              # noqa: E731
    is_start = lambda p: p[:4] == struct.pack("<I", MAGIC_IFRAME)      # noqa: E731
    sent = asyncio.run(run())
    assert any(is_audio(p) for p in sent), "the test must really mix audio in"
    inside = False
    for p in sent:
        if is_start(p):
            inside = len(p) == MEDIA_CHUNK
        elif is_audio(p):
            assert not inside, "audio inside a video frame"
        elif len(p) < MEDIA_CHUNK:
            inside = False


def test_late_joiner_waits_for_a_real_keyframe():
    """The frames after an old keyframe refer to frames the viewer never got."""
    from bcproxy.bcmedia import AccessUnit
    from bcproxy.media import StreamInfo, StreamSource

    async def run():
        src = StreamSource("t", "rtsp://h/101", StreamInfo("H265", 640, 360, 15))
        src._task = asyncio.create_task(asyncio.sleep(5))
        src.last_key = AccessUnit(b"old", True)
        q = src.subscribe()
        src._task.cancel()
        return q.qsize()

    assert asyncio.run(run()) == 0


def test_frame_rate_comes_from_the_camera_not_the_probe():
    """ffprobe said 60 for a Dahua stream running at 30."""
    from bcproxy.camera import VirtualCamera
    from bcproxy.media import StreamInfo

    async def run():
        cam = VirtualCamera.__new__(VirtualCamera)
        cam.cfg = SimpleNamespace(name="t")
        cam.main = SimpleNamespace(name="main", info=StreamInfo("H265", 2560, 1440, 60))
        cam.sub = SimpleNamespace(name="sub", info=StreamInfo("H265", 640, 480, 30))

        async def stream_state(idx):
            return ("H265", 0, 0, {1: 30, 2: 15}[idx], 0)
        cam.hikcfg = SimpleNamespace(stream_state=stream_state)
        await cam._use_camera_frame_rates()
        return cam.main.info, cam.sub.info

    main, sub = asyncio.run(run())
    assert (main.fps, sub.fps) == (30, 15) and (main.width, sub.height) == (2560, 480)


def test_unknown_camera_frame_rate_keeps_the_probe():
    from bcproxy.camera import VirtualCamera
    from bcproxy.media import StreamInfo

    async def run():
        cam = VirtualCamera.__new__(VirtualCamera)
        cam.cfg = SimpleNamespace(name="t")
        before = StreamInfo("H265", 640, 360, 15)
        cam.main, cam.sub = SimpleNamespace(info=before), None

        async def stream_state(idx):
            return ("H265", 0, 0, 0, 0)
        cam.hikcfg = SimpleNamespace(stream_state=stream_state)
        await cam._use_camera_frame_rates()
        return cam.main.info is before

    assert asyncio.run(run())


class _FakeFfmpeg:
    """stdout gives the chunks, then stays silent (or ends, when told to)."""

    def __init__(self, chunks, silent=True):
        self.chunks, self.silent, self.killed, self.returncode = list(chunks), silent, False, None
        self.stdout = SimpleNamespace(read=self._read)
        self.stderr = SimpleNamespace(read=self._err)
        self.pid = 1

    async def _read(self, n):
        if self.chunks:
            return self.chunks.pop(0)
        if self.silent and not self.killed:
            await asyncio.Event().wait()
        return b""

    async def _err(self):
        return b""

    def kill(self):
        self.killed, self.returncode = True, -9

    async def wait(self):
        return self.returncode


def _silent_stream_test(monkeypatch, make_source, first_chunks, later_chunks):
    from bcproxy import media
    monkeypatch.setattr(media, "STALL_SECONDS", 0.05)
    started = []
    plan = [_FakeFfmpeg(first_chunks), _FakeFfmpeg(later_chunks, silent=False)]

    async def spawn(*a, **kw):
        started.append(plan.pop(0) if plan else _FakeFfmpeg([], silent=False))
        return started[-1]
    monkeypatch.setattr(media, "_spawn", spawn)
    real_sleep = asyncio.sleep

    async def quick(t):
        await real_sleep(min(t, 0.01))
    monkeypatch.setattr(media.asyncio, "sleep", quick)

    async def run():
        src = make_source(media)
        q = src.subscribe()
        for _ in range(400):
            if len(started) >= 2 and q.qsize():
                break
            await real_sleep(0.005)
        got = q.qsize()
        src.unsubscribe(q)
        src._task.cancel()
        await asyncio.gather(src._task, return_exceptions=True)
        return got
    return started, asyncio.run(run())


def test_silent_video_stream_gets_a_new_ffmpeg(monkeypatch, caplog):
    """Seen on a real camera: its connection was gone, ffmpeg kept running and no frame ever came."""
    frame = b"\x00\x00\x00\x01\x65\x88" + bytes(50)      # first_mb_in_slice == 0: a new picture
    started, got = _silent_stream_test(
        monkeypatch,
        lambda media: media.StreamSource("cam/sub", "rtsp://h/102", media.StreamInfo("H264", 640, 360, 15), linger=0),
        [], [frame, frame, frame])
    assert len(started) >= 2 and started[0].killed
    assert got >= 1            # frames flow again, to the viewer who was already there
    assert "cam/sub: no data for" in caplog.text


def test_silent_audio_gets_a_new_ffmpeg(monkeypatch, caplog):
    adts = b"\xff\xf1\x60\x40\x08\x3f\xfc" + bytes(58)      # 65 bytes, as its header says
    started, got = _silent_stream_test(
        monkeypatch,
        lambda media: media.AudioSource("cam/audio", "rtsp://h/101", linger=0),
        [], [adts, adts])
    assert len(started) >= 2 and started[0].killed
    assert got >= 1
    assert "cam/audio: no data for" in caplog.text


def test_a_stream_that_keeps_coming_is_left_alone(monkeypatch):
    from bcproxy import media
    monkeypatch.setattr(media, "STALL_SECONDS", 0.5)
    proc = _FakeFfmpeg([b"a", b"b"])

    async def run():
        return [await media._read_live("t", proc, 10), await media._read_live("t", proc, 10)]

    assert asyncio.run(run()) == [b"a", b"b"] and not proc.killed


def test_noisy_stderr_does_not_block_ffmpeg():
    """Seen on a real camera: ffmpeg's dts complaints filled the unread pipe and the stream died after minutes."""
    from bcproxy import media
    script = ("import sys\n"
              "for i in range(4000):\n"
              "    sys.stderr.write('x' * 99 + chr(10)); sys.stderr.flush()\n"
              "sys.stderr.write('the last line' + chr(10)); sys.stderr.flush()\n"
              "sys.stdout.write('done'); sys.stdout.flush()\n")

    async def run():
        proc = await media._spawn(sys.executable, ["-c", script], "file.mp4", [])
        out = await asyncio.wait_for(proc.stdout.read(10), 30)
        await asyncio.wait_for(proc.wait(), 30)
        return out, await media._errors(proc)

    out, err = asyncio.run(run())
    assert out == b"done"
    assert err.endswith("the last line") and len(err) <= media.STDERR_KEEP
