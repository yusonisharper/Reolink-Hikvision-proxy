"""Dahua source camera: event stream, settings, recordings, against tools/fake_dahua.py."""
import asyncio
import datetime as dt
import importlib.util
from pathlib import Path

import pytest

from bcproxy import bc, dahua, session
from bcproxy.bc import xml_value
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.config import load
from bcproxy.events import Holds
from bcproxy.media import StreamInfo
from bcproxy.session import BcSession

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("fake_dahua", ROOT / "tools" / "fake_dahua.py")
fake_dahua = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fake_dahua)
FakeDahua = fake_dahua.FakeDahua
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

T0 = dt.datetime(2026, 11, 3, 8, 0, 0)


def _mins(a, b):
    return T0 + dt.timedelta(minutes=a), T0 + dt.timedelta(minutes=b)


def run(fake, coro_fn):
    """coro_fn(port) with the fake camera listening."""
    async def main():
        srv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
        try:
            return await coro_fn(srv.sockets[0].getsockname()[1])
        finally:
            srv.close()
    return asyncio.run(main())


def _camera(port=0, **kw):
    return VirtualCamera(CameraConfig(name="t", vendor="dahua", password="pw", hik_host="127.0.0.1", hik_port=port,
                                      hik_user="admin", hik_password="pw", main_rtsp="m.mp4",
                                      event_source="attach", ai_types=["people", "vehicle"],
                                      smart_zones={"crossline": [{"zone": 0, "name": "a"}, {"zone": 1, "name": "b"}],
                                                   "intrusion": [{"zone": 0, "name": "c"}]},
                                      snapshot_path="/cgi-bin/snapshot.cgi?channel=1", **kw))


# ------------------------------------------------------------------ event text
def test_parser_finds_events_in_every_framing():
    p = dahua.EventParser()
    plain = b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 37\r\n\r\nCode=VideoMotion;action=Start;index=0\r\n"
    no_blank = b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length:36\r\nCode=VideoBlind;action=Stop;index=2\r\n\r\n"
    beat = b"--myboundary\r\nContent-Type: text/plain\r\nContent-Length: 9\r\n\r\nHeartbeat\r\n"
    a, b = p.feed(plain + beat + no_blank)
    assert (a.event_type, a.state, a.channel) == ("VideoMotion", "active", 1)
    assert (b.event_type, b.state, b.channel) == ("VideoBlind", "inactive", 3)


def test_parser_waits_for_events_cut_by_a_chunk():
    text = fake_dahua.event_text("CrossLineDetection", "Pulse", fake_dahua.ivs_data(2, "Human"), index=10) + b"\r\n"
    p = dahua.EventParser()
    got = []
    for i in range(len(text)):       # the worst case: byte by byte
        got += p.feed(text[i:i + 1])
    assert len(got) == 1
    assert got[0].channel == 11 and got[0].state == "pulse" and got[0].regions == [(2, "Human")]


def test_parser_survives_data_that_is_not_json():
    p = dahua.EventParser()
    got = p.feed(b"Code=A;action=Start;index=0;data={bad\r\n--myboundary\r\nCode=B;action=Stop;index=0\r\n")
    assert [a.event_type for a in got] == ["A", "B"]


def test_mapping_to_reolink_events():
    cam = _camera()
    ev = lambda *a: cam.mapper.map(dahua.to_alert(*a))  # noqa: E731
    assert ev("HumanTrait", "Pulse", 0)[0].targets == {"people"}
    assert ev("VehicleDetect", "Pulse", 0)[0].targets == {"vehicle"}
    assert [(e.kind, e.active, e.targets) for e in ev("VideoMotion", "Start", 0)] == [("motion", True, frozenset())]
    assert ev("SmartMotionHuman", "Start", 0)[0].targets == {"people"}
    assert ev("SmartMotionVehicle", "Start", 0)[0].targets == {"vehicle"}
    line = ev("CrossLineDetection", "Pulse", 0, fake_dahua.ivs_data(2, "Vehicle"))[0]
    assert (line.kind, line.zone, line.targets) == ("crossline", 1, {"vehicle"})
    assert ev("CrossRegionDetection", "Start", 0, fake_dahua.ivs_data(1, "Human"))[0].kind == "intrusion"
    assert ev("WanderDetection", "Pulse", 0)[0].kind == "loitering"
    assert ev("VideoBlind", "Start", 0)[0].kind == "tamper"
    assert ev("VideoMotion", "Start", 1) == []       # another channel of the device
    assert ev("IntelliFrame", "Pulse", 0) == []      # chatter


def test_tracker_repeats_until_stop():
    t = dahua.EdgeTracker(stale_after=100)
    start = dahua.to_alert("VideoMotion", "Start", 0)
    assert t.feed(start, now=0) is start
    assert t.running(now=50) == [start]
    stop = dahua.to_alert("VideoMotion", "Stop", 0)
    assert t.feed(stop, now=51) is stop and t.running(now=51) == []
    # a lost Stop does not leave it on for good
    t.feed(start, now=60)
    assert t.running(now=161) == []


def test_tracker_pulses_and_smart_stops_keep_their_hold():
    t = dahua.EdgeTracker()
    pulse = dahua.to_alert("CrossLineDetection", "Pulse", 0, fake_dahua.ivs_data(1, "Human"))
    assert t.feed(pulse) is pulse and t.running() == []
    r1 = dahua.to_alert("CrossRegionDetection", "Start", 0, fake_dahua.ivs_data(1, "Human"))
    r2 = dahua.to_alert("CrossRegionDetection", "Start", 0, fake_dahua.ivs_data(2, "Human"))
    t.feed(r1), t.feed(r2)
    assert t.feed(dahua.to_alert("CrossRegionDetection", "Stop", 0, fake_dahua.ivs_data(1, "Human"))) is None
    assert t.running() == [r2]
    t.feed(dahua.to_alert("CrossRegionDetection", "Stop", 0))       # without data: every rule
    assert t.running() == []


def test_tracker_motion_stop_waits_for_smart_motion():
    t = dahua.EdgeTracker()
    t.feed(dahua.to_alert("VideoMotion", "Start", 0))
    t.feed(dahua.to_alert("SmartMotionHuman", "Start", 0))
    assert t.feed(dahua.to_alert("VideoMotion", "Stop", 0)) is None   # would blink the person off
    assert t.feed(dahua.to_alert("SmartMotionHuman", "Stop", 0)) is None
    assert t.running() == []


# ----------------------------------------------------------------- live stream
def test_event_stream_keeps_state_until_stop_and_reconnects():
    fake = FakeDahua("admin", "pw")

    async def go(port):
        cam = _camera(port, holds=Holds(motion=0.15, ai=0.15, smart=0.1))
        task = asyncio.create_task(dahua.event_stream(cam.http, cam.on_alert, repeat_interval=0.03))
        for _ in range(100):
            if fake.streams:
                break
            await asyncio.sleep(0.01)
        fake.emit(fake_dahua.event_text("SmartMotionHuman"))
        fake.emit(b"Heartbeat")
        await asyncio.sleep(0.5)     # far longer than the hold: Start only came once
        snap = cam.state.snapshot()
        assert snap.motion and snap.ai == {"people"}
        fake.emit(fake_dahua.event_text("SmartMotionHuman", "Stop"))
        await asyncio.sleep(0.4)
        assert not cam.state.snapshot().motion
        fake.emit(fake_dahua.event_text("CrossLineDetection", "Pulse", fake_dahua.ivs_data(2, "Human")))
        await asyncio.sleep(0.05)
        assert ("crossline", 1, "people") in cam.state.snapshot().smart
        await asyncio.sleep(0.2)
        assert not cam.state.snapshot().smart
        for w in list(fake.streams):
            w.close()
        fake.streams.clear()
        for _ in range(300):
            if fake.streams:
                break
            await asyncio.sleep(0.01)
        reconnected = bool(fake.streams)
        task.cancel()
        return reconnected

    assert run(fake, go)


# -------------------------------------------------------------------- settings
def test_capabilities_and_streams_from_the_camera():
    fake = FakeDahua("admin", "pw")

    async def go(port):
        cam = _camera(port)
        await cam.hikcfg.load()
        caps = cam.hikcfg.caps
        assert caps[1].sizes == [(2688, 1520), (1920, 1080), (1280, 720)]
        assert (caps[1].max_fps, caps[1].max_kbps) == (25, 8192)
        assert caps[2].sizes == [(704, 576), (352, 288)] and caps[2].max_kbps == 1024
        assert cam.hikcfg.bitrate_mode == {1: "vbr", 2: "vbr"} and cam.hikcfg.has_tamper
        assert await cam.hikcfg.audio_enabled()
        assert await cam.hikcfg.bitrates() == (4096, 512)
        assert await cam.hikcfg.stream_state(1) == ("H265", 2688, 1520, 25, 4096)
        assert await cam.hikcfg.stream_state(2) == ("H264", 704, 576, 15, 512)
        await cam.hikcfg.set_stream(1, 1920, 1080, 20, 2048, audio=False)
        assert await cam.hikcfg.stream_state(1) == ("H265", 1920, 1080, 20, 2048)
        assert not await cam.hikcfg.audio_enabled()
        with pytest.raises(ValueError):
            await cam.hikcfg.set_stream(1, 999, 999, 20, 2048)
        await cam.hikcfg.set_bitrate_mode("cbr")
        assert fake.tables["Encode[0].ExtraFormat[0].Video.BitRateControl"] == "CBR"
        await cam.hikcfg.set_channel_name("前门 & yard")
        await cam.hikcfg.set_tamper(True)
        return await cam.hikcfg.tamper_enabled()

    assert run(fake, go)
    assert fake.tables["ChannelTitle[0].Name"] == "前门 & yard"


def _image_doc(**values):
    return ('<body><VideoInput version="1.1"><bright>{bright}</bright><contrast>{contrast}</contrast>'
            "<saturation>128</saturation><sharpen>128</sharpen></VideoInput>"
            '<InputAdvanceCfg version="1.1"><mirror>{mirror}</mirror><flip>0</flip>'
            "<DayNight><mode>{day}</mode></DayNight><encType>{enc}</encType>"
            "<constantFrameRate>0</constantFrameRate></InputAdvanceCfg></body>").format(**values).encode()


def test_image_page_reads_and_writes():
    fake = FakeDahua("admin", "pw")
    fake.tables["VideoColor[0][0].Brightness"] = "20"
    fake.tables["VideoInOptions[0].Mirror"] = "true"
    fake.tables["VideoInDayNight[0][0].Mode"] = "BlackWhite"
    template = _image_doc(bright=0, contrast=0, mirror=0, day="auto", enc=1)

    async def go(port):
        cam = _camera(port)
        await cam.hikcfg.load()
        doc = (await cam.hikcfg.image_xml(template, 1)).decode()
        assert "<bright>51</bright>" in doc and "<contrast>128</contrast>" in doc
        assert "<mirror>1</mirror>" in doc and "<mode>blackAndWhite</mode>" in doc
        assert "<constantFrameRate>1</constantFrameRate>" in doc and "<encType>0</encType>" in doc
        same = _image_doc(bright=128, contrast=128, mirror=0, day="color", enc=1)
        changed = await cam.hikcfg.apply_image(same)
        assert changed == ["Brightness", "Mirror", "DayNight", "bitrate mode cbr"]
        return await cam.hikcfg.apply_image(same)

    assert run(fake, go) == []                                # second save: nothing to do
    assert all(fake.tables[f"VideoColor[0][{p}].Brightness"] == "50" for p in range(3))
    assert all(fake.tables[f"VideoInDayNight[0][{p}].Mode"] == "Color" for p in range(3))
    assert fake.tables["VideoInOptions[0].Mirror"] == "false"


def test_image_page_skips_tables_the_model_lacks():
    fake = FakeDahua("admin", "pw")
    fake.tables = {k: v for k, v in fake.tables.items() if not k.startswith(("VideoInSharpness", "VideoInDayNight"))}
    before = _image_doc(bright=128, contrast=128, mirror=0, day="color", enc=0)

    async def go(port):
        cam = _camera(port)
        await cam.hikcfg.load()
        assert await cam.hikcfg.apply_image(before) == []
        assert "<mode>color</mode>" in (await cam.hikcfg.image_xml(before)).decode()

    run(fake, go)
    assert fake.sets == []


def test_status_poll_and_snapshot_and_rename():
    fake = FakeDahua("admin", "pw")

    async def go(port):
        cam = _camera(port)
        poll = asyncio.create_task(cam._poll_status(interval=60))
        for _ in range(100):
            if cam.hik_bitrate is not None:
                break
            await asyncio.sleep(0.01)
        poll.cancel()
        assert (cam.hik_bitrate, cam.hik_sub_bitrate, cam.hik_cpu) == (4096, 512, None)
        jpeg = await cam.snapshot_jpeg()
        cam.rename("New name")
        await asyncio.gather(*cam._tasks)
        return jpeg

    assert run(fake, go) == fake.jpeg
    assert fake.tables["ChannelTitle[0].Name"] == "New name"


# ------------------------------------------------------------------ recordings
def test_search_on_one_connection_with_types_and_paging():
    recs = [(*_mins(i * 2, i * 2 + 1), "Event", "VideoMotion") for i in range(150)]
    recs += [(*_mins(400, 410), "Timing", ""), (*_mins(420, 421), "Event", "SmartMotionHuman")]
    fake = FakeDahua("admin", "pw", recordings=recs)

    async def go(port):
        rec = _camera(port).recordings
        found = await rec.search(T0, T0 + dt.timedelta(days=1))
        assert await rec.search(T0, T0 + dt.timedelta(days=1)) is found   # cached for the app's repeat visits
        first = await rec.search(T0 + dt.timedelta(minutes=399), T0 + dt.timedelta(days=1), limit=1)
        return found, first

    found, first = run(fake, go)
    assert len(found) == 152 and found[0].rec_type == "md"
    assert [s.rec_type for s in found[-2:]] == ["sched", "people,md"]
    assert first[0].rec_type == "sched"
    assert not fake.finders                     # search objects are released
    assert any("%20" in r and "StartTime=2026-11-03%2008:00:00" in r for r in fake.requests)


def test_record_days_and_storage():
    fake = FakeDahua("admin", "pw", recordings=[(*_mins(0, 1), "Timing", "")])

    async def go(port):
        rec = _camera(port).recordings
        days = await rec.record_days(2026, 11)
        count = len(fake.requests)
        assert await rec.record_days(2026, 11) == days and len(fake.requests) == count     # cached
        return days, await rec.storage()

    days, storage = run(fake, go)
    assert days == {3} and storage == (2048, 1024)


def test_no_card_means_no_playback():
    fake = FakeDahua("admin", "pw")
    real_reply = fake.reply
    fake.reply = lambda path, conn: (("400 Bad Request", "text/plain", b"Error\r\n")
                                     if "storageDevice" in path else real_reply(path, conn))

    async def go(port):
        cam = _camera(port)
        assert await cam._load_hik()      # an answer, unlike an unreachable camera
        return cam.recordings

    assert run(fake, go) is None


def test_replay_url():
    rec = _camera().recordings
    url = asyncio.run(rec.replay_url("rtsp://u:p%40ss@10.0.0.2:554/cam/realmonitor?channel=1&subtype=0",
                                     T0, T0 + dt.timedelta(minutes=5)))
    assert url == ("rtsp://u:p%40ss@10.0.0.2:554/cam/playback?channel=1"
                   "&starttime=2026_11_03_08_00_00&endtime=2026_11_03_08_05_00")


def test_second_channel_uses_its_own_indexes():
    fake = FakeDahua("admin", "pw")

    async def go(port):
        cam = _camera(port, hik_channel=2)
        assert cam.hikcfg.ch == 1
        assert cam.mapper.map(dahua.to_alert("VideoMotion", "Start", 1))
        assert not cam.mapper.map(dahua.to_alert("VideoMotion", "Start", 0))
        return cam.hikcfg._stream_key(1, "Video.FPS")

    assert run(fake, go) == "Encode[1].MainFormat[0].Video.FPS"


# ---------------------------------------------------------------- app messages
def _t(tag, h=0, mi=0, s=0, d=3):
    return (f"<{tag}>\n<year>2026</year>\n<month>11</month>\n<day>{d}</day>\n<hour>{h}</hour>\n"
            f"<minute>{mi}</minute>\n<second>{s}</second>\n</{tag}>\n")


def test_app_messages_end_to_end(monkeypatch):
    fake = FakeDahua("admin", "pw", recordings=[(*_mins(0, 10), "Event", "SmartMotionHuman")])
    urls = []

    async def fake_aus(url, codec):
        urls.append(url)
        return
        yield

    monkeypatch.setattr(session, "read_access_units", fake_aus)

    async def go(port):
        cam = _camera(port, listen_host="127.0.0.1")
        cam.cfg.main_rtsp = "rtsp://u:p@h:554/cam/realmonitor?channel=1&subtype=0"
        cam.main.info = cam.sub.info = StreamInfo("H264", 640, 360, 10)
        await cam._load_hik()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        out = {"hdd": (await c.request(102)).payload}
        out["days"] = (await c.request(142, payload=bc.body_xml(
            '<DayRecords version="1.1">\n' + _t("startTime", d=1) + _t("endTime", 23, 59, 59, d=30)
            + "<DayRecordList>\n<DayRecord>\n<index>0</index>\n<channelId>0</channelId>\n</DayRecord>\n"
            "</DayRecordList>\n</DayRecords>"))).payload
        r = await c.request(14, payload=bc.body_xml(
            '<FileInfoList version="1.1">\n<FileInfo>\n<channelId>0</channelId>\n<streamType>mainStream</streamType>\n'
            + _t("startTime") + _t("endTime", 23, 59, 59) + "</FileInfo>\n</FileInfoList>"))
        q = (f'<FileInfoList version="1.1">\n<FileInfo>\n<channelId>0</channelId>\n'
             f'<handle>{xml_value(r.payload, "handle")}</handle>\n</FileInfo>\n</FileInfoList>')
        out["files"] = (await c.request(15, payload=bc.body_xml(q))).payload
        out["replay"] = (await c.request(381, payload=bc.body_xml(
            '<ReplayByTimeV2 version="1.1">\n<seq>5</seq>\n' + _t("startTime", 8, 1) + _t("endTime", 23, 59, 59)
            + "<streamType>mainStream</streamType>\n</ReplayByTimeV2>"))).response
        for _ in range(100):
            if urls:
                break
            await asyncio.sleep(0.01)
        await c.request(382)
        # record settings are not connected: the app gets an answer, the camera no write
        out["record"] = (await c.request(81)).response
        out["record_set"] = (await c.request(82, payload=bc.body_xml(
            '<Record version="1.1">\n<channelId>0</channelId>\n<enable>0</enable>\n</Record>'))).response
        out["image"] = (await c.request(132)).payload
        out["image_set"] = (await c.request(25, payload=bc.body_xml(
            '<VideoInput version="1.1">\n<bright>255</bright>\n</VideoInput>'))).response
        c.writer.close()
        srv.close()
        return out

    out = run(fake, go)
    assert xml_value(out["hdd"], "capacity") == "2" and xml_value(out["hdd"], "remainSize") == "1"
    assert b"<index>2</index>" in out["days"]
    assert b"<recordType>people,md</recordType>" in out["files"] and b"<name>0120261103080000</name>" in out["files"]
    assert out["replay"] == 200
    assert urls[0] == ("rtsp://u:p@h:554/cam/playback?channel=1"
                       "&starttime=2026_11_03_08_01_00&endtime=2026_11_03_08_10_00")
    assert out["record"] == 200 and out["record_set"] == 200
    assert b"<bright>128</bright>" in out["image"]
    assert out["image_set"] == 200
    assert fake.sets == [{f"VideoColor[0][{p}].Brightness": "100" for p in range(3)}]


# ------------------------------------------------------------------------- PTZ
def _ptz_ctl(command, speed=None):
    extra = f"<speed>{speed}</speed>\n" if speed is not None else ""
    return bc.body_xml(f'<PtzControl version="1.1">\n<channelId>0</channelId>\n{extra}<command>{command}</command>\n</PtzControl>')


def _preset(command, preset=9):
    return bc.body_xml(f'<PtzPreset version="1.1">\n<channelId>0</channelId>\n<presetList>\n<preset>\n<id>{preset}</id>\n'
                       f"<command>{command}</command>\n</preset>\n</presetList>\n</PtzPreset>")


def run_ptz(fake, script, **kw):
    """script(client, cam) with an app logged in on a session of the camera."""
    async def go(port):
        cam = _camera(port, listen_host="127.0.0.1", **kw)
        await cam.hikcfg.load()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        try:
            return await script(c, cam)
        finally:
            if not c.writer.is_closing():
                c.writer.close()
            srv.close()
    return run(fake, go)


def test_ptz_moves_and_speed():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        got = [(await c.request(18, payload=_ptz_ctl(cmd, speed))).response for cmd, speed in (
            ("left", 1), ("stop", None), ("rightUp", 64), ("stop", None), ("zoomIn", None), ("stop", None), ("down", 32.0))]
        await c.request(18, payload=_ptz_ctl("stop"))
        return got

    assert run_ptz(fake, script) == [200] * 7
    assert fake.ptz_cmds == [("start", "Left", "0", "1", "0"), ("stop", "Left", "0", "0", "0"),
                             ("start", "RightUp", "8", "8", "0"), ("stop", "RightUp", "0", "0", "0"),
                             ("start", "ZoomTele", "0", "4", "0"), ("stop", "ZoomTele", "0", "0", "0"),
                             ("start", "Down", "0", "4", "0"), ("stop", "Down", "0", "0", "0")]


def test_ptz_direction_change_stops_the_old_move_first():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        await c.request(18, payload=_ptz_ctl("left", 8))
        await c.request(18, payload=_ptz_ctl("left", 8))    # same direction: not stopped between
        await c.request(18, payload=_ptz_ctl("up", 8))
        await c.request(18, payload=_ptz_ctl("stop"))

    run_ptz(fake, script)
    assert [(a, b) for a, b, *_ in fake.ptz_cmds] == [("start", "Left"), ("start", "Left"), ("stop", "Left"),
                                                      ("start", "Up"), ("stop", "Up")]


def test_ptz_stops_when_the_client_disconnects():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        await c.request(18, payload=_ptz_ctl("left", 8))
        c.writer.close()
        for _ in range(200):
            if cam._ptz_code is None:
                break
            await asyncio.sleep(0.01)

    run_ptz(fake, script)
    assert fake.ptz_cmds[-1][:2] == ("stop", "Left")


def test_ptz_stops_by_itself_without_a_stop():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        await c.request(18, payload=_ptz_ctl("left", 8))
        await asyncio.sleep(0.2)
        assert cam._ptz_code is None

    run_ptz(fake, script, ptz_max_seconds=0.05)
    assert fake.ptz_cmds[-1][:2] == ("stop", "Left")


def test_ptz_stop_is_retried():
    fake = FakeDahua("admin", "pw")
    fake.ptz_fail_stops = 1

    async def script(c, cam):
        await c.request(18, payload=_ptz_ctl("left", 8))
        return (await c.request(18, payload=_ptz_ctl("stop"))).response

    assert run_ptz(fake, script) == 200
    assert fake.ptz_cmds[-1][:2] == ("stop", "Left")


def test_ptz_presets_list_go_save_delete():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        listed = (await c.request(190)).payload
        go = (await c.request(19, payload=_preset("toPos"))).response
        save = (await c.request(19, payload=_preset("setPos", 5).replace(
            b"<command>", b"<name>New one</name>\n<command>"))).response
        delete = (await c.request(19, payload=_preset("delPos", 1))).response
        unknown = (await c.request(18, payload=_ptz_ctl("spin"))).response
        hidden = (await c.request(19, payload=_preset("setPos", 129))).response
        return listed, go, save, delete, unknown, hidden

    listed, go, save, delete, unknown, hidden = run_ptz(fake, script)
    assert b"<id>1</id>\n<name>Gate</name>" in listed and b"<name>A &amp; B</name>" in listed
    assert (go, save, delete, unknown, hidden) == (200, 200, 200, 400, 400)
    assert fake.ptz_cmds == [("start", "GotoPreset", "0", "9", "0"), ("start", "SetPreset", "0", "5", "0"),
                             ("start", "ClearPreset", "0", "1", "0")]
    assert fake.tables["PtzPreset[0][4].Name"] == "New one"
    assert (1, "Gate") not in fake.presets


def test_new_preset_from_the_app_takes_a_free_id_and_overwrites_nothing():
    fake = FakeDahua("admin", "pw")
    fake.presets = [(1, "Gate"), (2, "Road"), (4, "Far")]

    def new(name):
        return _preset("setPos", 0).replace(b"<command>", f"<name>{name}</name>\n<command>".encode())

    async def script(c, cam):
        first = (await c.request(19, payload=new("Three"))).response
        second = (await c.request(19, payload=new("Five"))).response
        fake.presets = [(i, f"P{i}") for i in range(1, 129)]
        return first, second, (await c.request(19, payload=new("Full"))).response

    assert run_ptz(fake, script) == (200, 200, 400)
    saved = [cmd[3] for cmd in fake.ptz_cmds if cmd[1] == "SetPreset"]
    assert saved == ["3", "5"]
    assert fake.tables["PtzPreset[0][2].Name"] == "Three" and fake.tables["PtzPreset[0][4].Name"] == "Five"


def test_presets_beyond_128_are_hidden():
    fake = FakeDahua("admin", "pw")
    fake.presets = [(i, f"P{i}") for i in range(1, 301)]

    async def script(c, cam):
        return (await c.request(190)).payload

    listed = run_ptz(fake, script)
    assert listed.count(b"<preset>") == 128 and b"<id>128</id>" in listed and b"<id>129</id>" not in listed


def _cruise(patrol=2, *points, name="Round"):
    keys = "".join(f"<keyPos>\n<keyPosId>{i}</keyPosId>\n<presetId>{p}</presetId>\n<speed>32</speed>\n"
                   f"<dwellTime>{d}</dwellTime>\n</keyPos>\n" for i, (p, d) in enumerate(points))
    return bc.body_xml(f'<PtzCruise version="1.1">\n<channelId>0</channelId>\n<cruiseList>\n<cruise>\n'
                       f"<patrolId>{patrol}</patrolId>\n<enable>1</enable>\n<name>{name}</name>\n"
                       f"<keyPosList>\n{keys}</keyPosList>\n</cruise>\n</cruiseList>\n</PtzCruise>")


def test_patrol_save_read_start_stop(monkeypatch):
    fake = FakeDahua("admin", "pw")
    monkeypatch.setattr(session, "MSG_CRUISE_READS", {197})

    async def script(c, cam):
        save = (await c.request(198, payload=_cruise(1, (1, 10), (9, 20)))).response     # any id: known by its tag
        read = (await c.request(197)).payload
        again = (await c.request(198, payload=_cruise(1, (9, 5)))).response              # a shorter list clears the rest
        start = (await c.request(18, payload=bc.body_xml(
            '<PtzControl version="1.1">\n<channelId>0</channelId>\n<command>startPatrol</command>\n'
            "<patrolId>1</patrolId>\n</PtzControl>"))).response
        running = cam.ptz_tour
        stop = (await c.request(18, payload=bc.body_xml(
            '<PtzControl version="1.1">\n<channelId>0</channelId>\n<command>stopPatrol</command>\n</PtzControl>'))).response
        bad = (await c.request(198, payload=_cruise(99, (1, 10)))).response
        return save, read, again, start, running, stop, cam.ptz_tour, bad

    save, read, again, start, running, stop, after, bad = run_ptz(fake, script)
    assert (save, again, start, stop, bad) == (200, 200, 200, 200, 400)
    assert read.count(b"<cruise>") == 1 and b"<patrolId>1</patrolId>" in read and b"<name>Round</name>" in read
    assert b"<presetId>9</presetId>\n<speed>32</speed>\n<dwellTime>20</dwellTime>" in read
    assert running == 2 and after is None
    assert fake.tables["PtzTour[0][1].Presets[0][0]"] == "8" and fake.tables["PtzTour[0][1].Presets[1][0]"] == "-1"
    assert fake.ptz_cmds == [("start", "StartTour", "2", "0", "0"), ("start", "StopTour", "2", "0", "0")]


def test_app_patrol_0_is_the_first_tour_and_editing_it_adds_nothing():
    fake = FakeDahua("admin", "pw")
    # as the app writes it: patrol 0 and every point slot, the unused ones with presetId -1 and nothing else
    blank = "".join(f"<keyPos>\n<keyPosId>{i}</keyPosId>\n<presetId>-1</presetId>\n</keyPos>\n" for i in range(2, 32))
    save = lambda name: _cruise(0, (1, 20), (9, 20), name=name).replace(b"</keyPosList>", blank.encode() + b"</keyPosList>")  # noqa: E731
    control = lambda command: bc.body_xml(  # noqa: E731
        f'<PtzControl version="1.1">\n<channelId>0</channelId>\n<speed>3</speed>\n<command>{command}</command>\n'
        "<patrolId>0</patrolId>\n</PtzControl>")

    async def script(c, cam):
        first = (await c.request(20, payload=save("One"))).response
        edit = (await c.request(20, payload=save("Renamed"))).response
        read = (await c.request(64)).payload
        return first, edit, read, (await c.request(18, payload=control("startPatrol"))).response, \
            (await c.request(18, payload=control("stopPatrol"))).response

    first, edit, read, start, stop = run_ptz(fake, script)
    assert (first, edit, start, stop) == (200, 200, 200, 200)
    assert read.count(b"<cruise>") == 1 and read.count(b"<keyPos>") == 2 and b"<presetId>-1" not in read
    assert b"<patrolId>0</patrolId>" in read and b"<name>Renamed</name>" in read
    assert b"<presetId>1</presetId>" in read and b"<presetId>9</presetId>" in read
    # the table counts presets from 0: preset 1 is a real point stored as 0, -1 is an empty one
    assert [fake.tables[f"PtzTour[0][0].Presets[{i}][0]"] for i in range(3)] == ["0", "8", "-1"]
    assert fake.ptz_cmds == [("start", "StartTour", "1", "0", "0"), ("start", "StopTour", "1", "0", "0")]


def test_ptz_does_not_wait_for_slow_requests():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        release = asyncio.Event()

        async def slow_days(year, month):
            await release.wait()
            return set()

        cam.recordings.record_days = slow_days
        await c.request(18, payload=_ptz_ctl("left", 8))
        days = asyncio.create_task(c.request(142, payload=bc.body_xml(
            '<DayRecords version="1.1">\n' + _t("startTime", d=1) + _t("endTime", 23, 59, 59, d=30)
            + "<DayRecordList>\n<DayRecord>\n<index>0</index>\n<channelId>0</channelId>\n</DayRecord>\n"
            "</DayRecordList>\n</DayRecords>"), timeout=10))
        await asyncio.sleep(0.05)
        stopped = (await c.request(18, payload=_ptz_ctl("stop"))).response   # while the search is still running
        was_running = not days.done()
        release.set()
        await days
        return stopped, was_running, cam._ptz_code

    assert run_ptz(fake, script) == (200, True, None)


def test_ptz_messages_keep_their_order():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        for command in ("left", "stop", "up", "stop"):
            await c.request(18, payload=_ptz_ctl(command, 8), wait=False)
        for _ in range(200):
            if len(fake.ptz_cmds) == 4:
                break
            await asyncio.sleep(0.01)

    run_ptz(fake, script)
    assert [c[:2] for c in fake.ptz_cmds] == [("start", "Left"), ("stop", "Left"), ("start", "Up"), ("stop", "Up")]


def test_offline_camera_does_not_delay_the_others():
    from bcproxy.__main__ import start_camera

    async def main():
        blocked = asyncio.Event()

        class Stuck(VirtualCamera):
            async def start(self):
                await blocked.wait()

        class Quick(VirtualCamera):
            async def start(self):
                pass

        stuck = Stuck(CameraConfig(name="stuck", listen_host="127.0.0.1", listen_port=0))
        quick = Quick(CameraConfig(name="quick", listen_host="127.0.0.1", listen_port=0))
        servers = []
        tasks = [asyncio.create_task(start_camera(c, False, servers)) for c in (stuck, quick)]
        await asyncio.wait_for(tasks[1], 2)      # listening while the first is still waiting
        early = len(servers)
        blocked.set()
        await tasks[0]
        late = len(servers)
        for s in servers:
            s.close()
        return early, late

    assert asyncio.run(main()) == (1, 2)


def test_port_in_use_only_stops_that_camera(caplog):
    from bcproxy.__main__ import start_camera

    class Quick(VirtualCamera):
        async def start(self):
            pass

    async def main():
        taken = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = taken.sockets[0].getsockname()[1]
        servers = []
        await start_camera(Quick(CameraConfig(name="clash", listen_host="127.0.0.1", listen_port=port)), False, servers)
        taken.close()
        return servers

    if asyncio.run(main()):      # Windows may allow a shared bind: nothing to assert then
        return
    assert "cannot listen" in caplog.text


def test_filter_lists_every_type():
    from bcproxy import playback as pb
    t = dt.datetime(2026, 11, 3)
    doc = pb.file_open_xml(1, t, t)
    listed = xml_value(doc.encode(), "recordType").split(",")
    assert {"md", "people", "vehicle", "crossline", "intrusion", "loitering"} <= set(listed)
    assert pb.join_types("people", "intrusion", "md") == "people,intrusion,md"
    assert pb.join_types("sched", "md") == "md" and pb.join_types("sched") == "sched"


def _zoom_to(pos, command="zoomPos"):
    return bc.body_xml('<StartZoomFocus version="1.1">\n<channelId>0</channelId>\n'
                       f"<command>{command}</command>\n<movePos>{pos}</movePos>\n</StartZoomFocus>")


def test_zoom_to_a_position_stops_near_it():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        read = (await c.request(294)).payload
        tele = (await c.request(295, payload=_zoom_to(5000))).response
        await cam._zoom_task
        at_tele = fake.zoom_value
        wide = (await c.request(295, payload=_zoom_to(1000))).response
        await cam._zoom_task
        return read, tele, at_tele, wide, (await c.request(295, payload=_zoom_to(5, "focusPos"))).response

    read, tele, at_tele, wide, focus = run_ptz(fake, script)
    assert (xml_value(read, "maxPos"), xml_value(read, "minPos"), xml_value(read, "curPos")) == ("16384", "0", "2500")
    assert (tele, wide, focus) == (200, 200, 400)
    assert abs(at_tele - 5000) <= 300 and abs(fake.zoom_value - 1000) <= 300 and not fake.zooming
    assert fake.ptz_cmds[-1][0] == "stop"


def test_ptz_panel_gets_a_patrol_list_from_msg_64():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        return await c.request(64)

    r = run_ptz(fake, script)
    assert r.response == 200 and b"<PtzCruise" in r.payload and b"<cruiseList>" in r.payload


def test_zoom_focus_read_stays_inside_the_range_it_declares():
    fake = FakeDahua("admin", "pw")
    fake.focus_position = 4680.57   # seen on a real camera, above the declared 1000

    async def script(c, cam):
        return (await c.request(294)).payload_text

    text = run_ptz(fake, script)
    focus = text[text.index("<focus>"):text.index("</focus>")]
    assert "<maxPos>1000</maxPos>" in focus and "<curPos>1000</curPos>" in focus


def test_zoom_already_there_does_not_move():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        response = (await c.request(295, payload=_zoom_to(2550))).response
        await cam._zoom_task
        return response

    assert run_ptz(fake, script) == 200 and fake.ptz_cmds == []


def test_zoom_stops_at_the_end_of_the_lens():
    fake = FakeDahua("admin", "pw")
    fake.zoom_limit = 3000        # the target is never reached

    async def script(c, cam):
        await c.request(295, payload=_zoom_to(4000))
        await cam._zoom_task
        return cam._ptz_code

    assert run_ptz(fake, script) is None
    assert fake.ptz_cmds[-1][:2] == ("stop", "ZoomTele") and fake.zoom_value == 3000
    assert len(fake.ptz_cmds) == 4     # one pulse, one try that did not move, then it gives up


def test_a_stop_or_new_move_takes_over_from_a_zoom():
    fake = FakeDahua("admin", "pw")

    async def script(c, cam):
        await c.request(295, payload=_zoom_to(16384))
        await asyncio.sleep(0.1)
        await c.request(18, payload=_ptz_ctl("left", 8))      # replaces the zoom
        await asyncio.sleep(0.3)
        kept = cam._ptz_code                                  # the old zoom must not stop this
        await c.request(18, payload=_ptz_ctl("stop"))
        await c.request(295, payload=_zoom_to(16384))
        await asyncio.sleep(0.1)
        await c.request(18, payload=_ptz_ctl("stop"))         # a stop ends the zoom for good
        await asyncio.sleep(0.2)
        return kept, cam._ptz_code

    assert run_ptz(fake, script) == ("Left", None)
    assert [c[:2] for c in fake.ptz_cmds] == [("start", "ZoomTele"), ("stop", "ZoomTele"),
                                              ("start", "Left"), ("stop", "Left"),
                                              ("start", "ZoomTele"), ("stop", "ZoomTele")]


def test_capability_bits_follow_the_camera():
    ptz, fixed = FakeDahua("admin", "pw"), FakeDahua("admin", "pw")
    fixed.has_ptz = False

    async def caps(port):
        cam = _camera(port)
        await cam.hikcfg.load()
        return cam

    tv = run(ptz, caps).template_vars()
    assert (tv["ptz_type"], tv["ptz_mode"], tv["ptz_preset"], tv["ptz_on"]) == (2, "pt", 1, 1) and "<PTZ>" in tv["ptz_ability"]
    assert tv["ptz_patrol"] == 1 and "cruise_rw" in tv["ptz_ability"]
    tv = run(fixed, caps).template_vars()
    assert (tv["ptz_type"], tv["ptz_mode"], tv["ptz_preset"], tv["ptz_on"]) == (0, "none", 0, 0) and not tv["ptz_ability"]
    hik = VirtualCamera(CameraConfig(name="h", hik_host="127.0.0.1")).template_vars()
    assert hik["ptz_type"] == 0 and hik["ptz_mode"] == "none"


def test_fixed_camera_does_not_answer_ptz():
    fake = FakeDahua("admin", "pw")
    fake.has_ptz = False

    async def script(c, cam):
        return (await c.request(18, payload=_ptz_ctl("left", 8))).response

    run_ptz(fake, script)
    assert fake.ptz_cmds == []


# ---------------------------------------------------------------------- wiring
def test_record_settings_are_not_offered():
    cam = _camera()
    assert "recordSchedule_rw" not in cam.template_vars()["record_ability"]
    assert "fileFind_rw" in cam.template_vars()["record_ability"]
    hik_cam = VirtualCamera(CameraConfig(name="h", hik_host="127.0.0.1", main_rtsp="m.mp4"))
    assert "recordSchedule_rw" in hik_cam.template_vars()["record_ability"]
    assert not hik_cam.hikcfg.unhandled_msgs


def test_config(tmp_path):
    path = tmp_path / "c.yaml"
    path.write_text("cameras:\n- name: d\n  dahua: {host: 10.0.0.2, channel: 2, main_rtsp: rtsp://x}\n"
                    "- name: h\n  hik: {host: 10.0.0.3}\n", encoding="utf-8")
    d, h = load(str(path)).cameras
    assert (d.vendor, d.hik_host, d.event_source) == ("dahua", "10.0.0.2", "attach")
    assert d.snapshot_path == "/cgi-bin/snapshot.cgi?channel=2"
    assert (h.vendor, h.event_source) == ("hikvision", "alertstream")
    assert h.snapshot_path == "/ISAPI/Streaming/channels/101/picture"

    path.write_text("cameras:\n- name: d\n  dahua: {host: a}\n  hik: {host: b}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load(str(path))
    path.write_text("cameras:\n- name: d\n  dahua: {host: a, events: listen}\n", encoding="utf-8")
    with pytest.raises(ValueError):
        load(str(path))
