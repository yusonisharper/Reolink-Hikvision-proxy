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
