"""Playback: app record search + replay against a fake Hikvision backend."""
import asyncio
import datetime as dt
import importlib.util
from pathlib import Path

from bcproxy import bc, session
from bcproxy import playback as pb
from bcproxy.bc import ext_xml, xml_value
from bcproxy.bcmedia import AccessUnit, BcMediaReader
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.media import StreamInfo
from bcproxy.session import BcSession

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'


def _item(s, e, kind="timing"):
    return (f"<searchMatchItem><timeSpan><startTime>{s}</startTime><endTime>{e}</endTime></timeSpan>"
            f"<mediaSegmentDescriptor><playbackURI>rtsp://x</playbackURI></mediaSegmentDescriptor>"
            f"<metadataMatches><metadataDescriptor>recordType.meta.hikvision.com/{kind}</metadataDescriptor>"
            f"</metadataMatches></searchMatchItem>")


class FakeHttp:
    """Camera in UTC-8: search/RTSP times are UTC, the app's are local."""

    def __init__(self):
        self.searches = []
        self.bodies = []

    async def post_bytes(self, path, body):
        if path.endswith("/dailyDistribution"):
            return 200, (f'<trackDailyDistribution {NS}><dayList>'
                         "<day><id>1</id><dayOfMonth>1</dayOfMonth><record>true</record></day>"
                         "<day><id>2</id><dayOfMonth>2</dayOfMonth><record>false</record></day>"
                         "<day><id>3</id><dayOfMonth>3</dayOfMonth><record>true</record></day>"
                         "</dayList></trackDailyDistribution>").encode()
        pos = int(body.split(b"<searchResultPostion>")[1].split(b"<")[0])
        self.searches.append(pos)
        self.bodies.append(body)
        if pos == 0:   # two pages: MORE, then OK
            return 200, (f"<CMSearchResult {NS}><responseStatusStrg>MORE</responseStatusStrg><matchList>"
                         + _item("2026-11-01T15:58:08Z", "2026-11-01T16:58:08Z")
                         + "</matchList></CMSearchResult>").encode()
        return 200, (f"<CMSearchResult {NS}><responseStatusStrg>OK</responseStatusStrg><matchList>"
                     + _item("2026-11-02T01:00:00+08:00", "2026-11-02T01:00:30+08:00", "motion")
                     + "</matchList></CMSearchResult>").encode()

    async def get_bytes(self, path):
        if path == "/ISAPI/System/time":
            return f"<Time {NS}><localTime>2026-11-01T23:56:11-08:00</localTime></Time>".encode()
        return (f'<storage {NS}><hddList><hdd><id>1</id><capacity>59200</capacity>'
                "<freeSpace>6050</freeSpace></hdd></hddList></storage>").encode()


def test_hik_search_pagination_and_types():
    rec = pb.HikRecordings(FakeHttp())
    segs = asyncio.run(rec.search(dt.datetime(2026, 11, 1), dt.datetime(2026, 11, 1, 23, 59, 59)))
    assert [(s.start, s.end, s.rec_type) for s in segs] == [
        (dt.datetime(2026, 11, 1, 7, 58, 8), dt.datetime(2026, 11, 1, 8, 58, 8), "sched"),
        (dt.datetime(2026, 11, 1, 9, 0, 0), dt.datetime(2026, 11, 1, 9, 0, 30), "md")]
    assert rec.http.searches == [0, 1]
    assert b"<startTime>2026-11-01T08:00:00Z</startTime>" in rec.http.bodies[0]
    assert b"<endTime>2026-11-02T07:59:59Z</endTime>" in rec.http.bodies[0]
    assert asyncio.run(rec.storage()) == (59200, 6050)
    assert asyncio.run(rec.record_days(2026, 11)) == {1, 3}


def test_replay_url():
    rec = pb.HikRecordings(FakeHttp())
    url = asyncio.run(rec.replay_url("rtsp://admin:p%40ss@10.0.0.2:554/Streaming/Channels/101",
                                     dt.datetime(2026, 11, 1, 7, 58, 8), dt.datetime(2026, 11, 1, 23, 59, 59)))
    assert url == ("rtsp://admin:p%40ss@10.0.0.2:554/Streaming/tracks/101"
                   "?starttime=20261101T155808Z&endtime=20261102T075959Z")
    assert pb.parse_offset("2026-11-01T23:56:11+05:30") == dt.timedelta(hours=5, minutes=30)
    assert pb.parse_offset("2026-11-01T23:56:11") is None


def _t(tag, y, mo, d, h=0, mi=0, s=0):
    return (f"<{tag}>\n<year>{y}</year>\n<month>{mo}</month>\n<day>{d}</day>\n<hour>{h}</hour>\n"
            f"<minute>{mi}</minute>\n<second>{s}</second>\n</{tag}>\n")


class FakeMain:
    info = StreamInfo("H264", 640, 360, 10)

    async def ensure_info(self):
        return self.info


def test_app_playback_flow(monkeypatch):
    async def fake_aus(url, codec):
        assert "starttime=20261101T155808Z" in url
        yield AccessUnit(b"\x00\x00\x00\x01\x41" + bytes(100), False)   # skipped: before 1st key
        yield AccessUnit(b"\x00\x00\x00\x01\x65" + bytes(40000), True)
        for _ in range(3):
            yield AccessUnit(b"\x00\x00\x00\x01\x41" + bytes(500), False)
        await asyncio.sleep(10)

    monkeypatch.setattr(session, "read_access_units", fake_aus)
    seq = 1793605341

    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw"))
        cam.recordings = pb.HikRecordings(FakeHttp())
        cam.main = FakeMain()
        cam.cfg.main_rtsp = "rtsp://u:p@h/Streaming/Channels/101"
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        out = {}
        out["199"] = (await c.request(199)).payload
        out["58"] = (await c.request(58)).payload
        out["hdd"] = (await c.request(102)).payload
        out["days"] = (await c.request(142, payload=bc.body_xml(
            '<DayRecords version="1.1">\n' + _t("startTime", 2026, 11, 1) + _t("endTime", 2026, 11, 30, 23, 59, 59)
            + "<DayRecordList>\n<DayRecord>\n<index>0</index>\n<channelId>0</channelId>\n</DayRecord>\n"
            "</DayRecordList>\n</DayRecords>"))).payload
        r = await c.request(14, payload=bc.body_xml(
            '<FileInfoList version="1.1">\n<FileInfo>\n<channelId>0</channelId>\n<streamType>subStream</streamType>\n'
            + _t("startTime", 2026, 11, 1) + _t("endTime", 2026, 11, 1, 23, 59, 59) + "</FileInfo>\n</FileInfoList>"))
        handle = xml_value(r.payload, "handle")
        q = f'<FileInfoList version="1.1">\n<FileInfo>\n<channelId>0</channelId>\n<handle>{handle}</handle>\n' \
            "</FileInfo>\n</FileInfoList>"
        out["files"] = (await c.request(15, payload=bc.body_xml(q))).payload
        out["close"] = (await c.request(16, payload=bc.body_xml(q))).response
        out["cover"] = (await c.request(298, payload=bc.body_xml('<CoverPreview version="1.1"/>'))).response
        r = await c.request(381, payload=bc.body_xml(
            f'<ReplayByTimeV2 version="1.1">\n<seq>{seq}</seq>\n' + _t("startTime", 2026, 11, 1, 7, 0, 0)
            + _t("endTime", 2026, 11, 1, 23, 59, 59) + "<playSpeed>1</playSpeed>\n<streamType>subStream</streamType>\n"
            "</ReplayByTimeV2>"))
        rq = c.subs[(381, r.msg_num)]
        info = await asyncio.wait_for(rq.get(), 3)
        rd, kinds, seqs = BcMediaReader(), [], set()
        while kinds.count("I") + kinds.count("P") < 4:
            m = await asyncio.wait_for(rq.get(), 3)
            seqs.add(m.cls << 16 | m.response)
            kinds += [k for k, _, _ in rd.feed(m.payload)]
        out["stop"] = (await c.request(382)).response
        c.writer.close()
        srv.close()
        return out, r, info, kinds, seqs

    out, first, info, kinds, seqs = asyncio.run(run())
    assert all(f"<{t}>1</{t}>".encode() in out["58"] for t in ("record", "replay", "disk"))
    assert b"<diskNum>1</diskNum>" in out["199"] and b"<replayVersion>21</replayVersion>" in out["199"]
    assert b"<capacity>57</capacity>" in out["hdd"] and b"<remainSizeM>930</remainSizeM>" in out["hdd"]
    assert out["days"].count(b"<dayType>") == 2 and b"<index>2</index>" in out["days"]
    assert out["files"].count(b"<FileInfo>") == 2 and b"<name>0120261101075808</name>" in out["files"]
    assert b"<recordType>md</recordType>" in out["files"]
    assert out["close"] == 200 and out["cover"] == 400 and out["stop"] == 200
    assert first.response == 200 and not first.payload
    assert xml_value(info.payload, "seq") == str(seq) and xml_value(info.payload, "year") == "126"
    assert xml_value(info.payload, "hour") == "7" and xml_value(info.payload, "minute") == "58"
    assert kinds[:4] == ["I", "P", "P", "P"] and seqs == {seq}


def test_no_playback_without_hik():
    cam = VirtualCamera(CameraConfig(name="t"))
    assert cam.recordings is None
    tv = cam.template_vars()
    assert (tv["disk_num"], tv["replay_version"], tv["record_ability"]) == (0, 0, "")


def test_merge_segments_caps_list_and_keeps_coverage():
    t = dt.datetime(2026, 11, 1)
    segs = [pb.Segment(t + dt.timedelta(seconds=40 * i), t + dt.timedelta(seconds=40 * i + 20),
                       "md" if i % 7 == 0 else "sched") for i in range(500)]
    segs.insert(0, pb.Segment(t - dt.timedelta(hours=1), t + dt.timedelta(seconds=5)))   # clipped
    out = pb.merge_segments(segs, t, t + dt.timedelta(days=1), max_items=60)
    assert 0 < len(out) <= 60
    assert out[0].start == t and out[-1].end == segs[-1].end
    assert all(a.end <= b.start for a, b in zip(out, out[1:]))
    assert "md" in {s.rec_type for s in out}
    few = pb.merge_segments(segs[1:4], t, t + dt.timedelta(days=1))
    assert len(few) == 3      # 20 s gaps: kept apart when already short


def test_event_journal_labels_and_persists(tmp_path):
    p = tmp_path / "cam" / "events.jsonl"
    j = pb.EventJournal(p)
    t = (dt.datetime.now() - dt.timedelta(hours=1)).replace(microsecond=0)
    j.add({"vehicle"}, t)
    j.add({"vehicle"}, t + dt.timedelta(seconds=1))          # repeat of an active alert: dropped
    j.add({"people", "crossline"}, t + dt.timedelta(minutes=10))
    assert len(j.items) == 3
    seg = lambda a, b: pb.Segment(t + dt.timedelta(seconds=a), t + dt.timedelta(seconds=b), "md")  # noqa: E731
    assert j.label(seg(-20, -3)) == "vehicle,md"              # within slack
    assert j.label(seg(60, 120)) == "md"
    assert j.label(seg(590, 620)) == "people,crossline,md"    # most specific first, all kept
    j2 = pb.EventJournal(p)                                   # reloaded from disk
    assert j2.items == j.items
    old = pb.EventJournal(p, keep_days=0)                     # pruned on load
    assert old.items == [] and p.read_text() == ""


def test_alerts_feed_journal():
    from bcproxy import hik
    cam = VirtualCamera(CameraConfig(name="t", ai_types=["people", "vehicle"],
                                     smart_zones={"intrusion": [{"zone": 0}]}))
    cam.on_alert(hik.HikAlert("VMD", "active", 1))                          # plain motion: not logged
    cam.on_alert(hik.HikAlert("VMD", "active", 1, target="vehicle"))
    cam.on_alert(hik.HikAlert("fielddetection", "active", 1, [(1, "human")]))
    assert sorted(k for _, k in cam.journal.items) == ["intrusion", "people", "vehicle"]


def test_file_list_stays_under_the_app_message_limit():
    """726 clips of mixed types were listed as 67 entries, 41 KB: the app hung on that reply."""
    t = dt.datetime(2026, 11, 3)
    kinds = ["md", "vehicle,intrusion", "people,vehicle,intrusion", "vehicle"]
    segs = [pb.Segment(t + dt.timedelta(seconds=110 * i), t + dt.timedelta(seconds=110 * i + 20), kinds[i % 4])
            for i in range(726)]
    xml, listed = pb.file_list_limited(1, "subStream", segs, t, t + dt.timedelta(days=1))
    assert len(xml.encode()) <= pb.MAX_REPLY_BYTES and 0 < listed <= 60
    assert xml.count("<FileInfo>") == listed
    few, n = pb.file_list_limited(1, "subStream", segs[:5], t, t + dt.timedelta(days=1))
    assert n == 5 and few.count("<FileInfo>") == 5     # short lists stay as they are


def _replay_cam():
    cam = VirtualCamera(CameraConfig(name="t", password="pw", listen_host="127.0.0.1"))
    cam.recordings = pb.HikRecordings(FakeHttp())
    cam.main = FakeMain()
    cam.cfg.main_rtsp = "rtsp://u:p@h/Streaming/Channels/101"
    return cam


def test_replay_plays_one_clip_and_seek_starts_another(monkeypatch):
    urls = []

    async def fake_aus(url, codec):
        urls.append(url)
        yield AccessUnit(b"\x00\x00\x00\x01\x65" + bytes(100), True)
        yield AccessUnit(b"\x00\x00\x00\x01\x41" + bytes(100), False)

    monkeypatch.setattr(session, "read_access_units", fake_aus)
    seq, seq2 = 1793769300, 1793769347

    async def run():
        cam = _replay_cam()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        r = await c.request(381, payload=bc.body_xml(
            f'<ReplayByTimeV2 version="1.1">\n<seq>{seq}</seq>\n' + _t("startTime", 2026, 11, 1, 7, 0, 0)
            + _t("endTime", 2026, 11, 1, 9, 30, 0) + "<streamType>mainStream</streamType>\n</ReplayByTimeV2>"))
        q = c.subs[(381, r.msg_num)]
        seen, infos, rd, kinds = set(), [], BcMediaReader(), []
        while len(kinds) < 2:
            m = await asyncio.wait_for(q.get(), 3)
            if m.binary:
                seen.add(m.cls << 16 | m.response)
                kinds += [x[0] for x in rd.feed(m.payload)]
            elif b"replayStreamInfo" in m.payload:
                infos.append(m.payload)
        await asyncio.sleep(0.1)         # the clip ended: nothing else starts by itself
        one_stream = len(urls)
        s = await c.request(383, payload=bc.body_xml(
            f'<ReplaySeekV2 version="1.1">\n<seq>{seq2}</seq>\n' + _t("seekTime", 2026, 11, 1, 8, 30, 0)
            + "</ReplaySeekV2>"))
        after, info = set(), None
        while seq2 not in after:
            m = await asyncio.wait_for(q.get(), 3)
            if m.binary:
                after.add(m.cls << 16 | m.response)
            elif b"replayStreamInfo" in m.payload:
                info = m.payload
        stop = await c.request(382)
        bad = await c.request(383, payload=bc.body_xml(
            f'<ReplaySeekV2 version="1.1">\n<seq>{seq2}</seq>\n' + _t("seekTime", 2026, 11, 1, 8, 30, 0)
            + "</ReplaySeekV2>"))
        c.writer.close()
        srv.close()
        return seen, infos, one_stream, s.response, info, stop.response, bad.response

    seen, infos, one_stream, seek, info, stop, bad = asyncio.run(run())
    assert seen == {seq} and seek == 200 and stop == 200 and bad == 400
    assert len(infos) == 1 and xml_value(infos[0], "minute") == "58"
    assert one_stream == 1                     # no auto-advance to the 09:00 clip
    assert "starttime=20261101T155808Z&endtime=20261101T165808Z" in urls[0]     # exactly that clip
    assert xml_value(info, "seq") == str(seq2)
    assert "starttime=20261101T163000Z" in urls[1]                               # 08:30 local


def test_starting_a_replay_takes_one_search_request():
    http = FakeHttp()
    rec = pb.HikRecordings(http)
    asyncio.run(rec.search(dt.datetime(2026, 11, 1), dt.datetime(2026, 11, 1, 23), limit=1))
    assert len(http.bodies) == 1 and b"<maxResults>1</maxResults>" in http.bodies[0]


def test_full_list_is_cached_briefly():
    http = FakeHttp()
    rec = pb.HikRecordings(http)
    span = dt.datetime(2026, 11, 1), dt.datetime(2026, 11, 1, 23)

    async def run():
        first = await rec.search(*span)
        calls = len(http.bodies)
        again = await rec.search(*span)
        assert again == first and len(http.bodies) == calls
        rec._lists = {k: (v[0] - pb.LIST_CACHE_SECONDS - 1, v[1]) for k, v in rec._lists.items()}
        await rec.search(*span)
        assert len(http.bodies) > calls

    asyncio.run(run())


def test_read_access_units_treats_a_silent_source_as_ended(monkeypatch):
    """Hikvision keeps the RTSP session open at the end of a clip: don't wait for it forever."""
    import time
    from bcproxy import media

    class Out:
        calls = 0

        async def read(self, n):
            Out.calls += 1
            if Out.calls == 1:
                return b"\x00\x00\x00\x01\x65" + bytes(64)
            await asyncio.sleep(60)

    class Proc:
        stdout, stderr, returncode, pid = Out(), None, None, 1
        killed = False

        def kill(self):
            Proc.killed = True

        async def wait(self):
            return 0

    async def spawn(*a, **k):
        return Proc()

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)

    async def run():
        t0 = time.monotonic()
        async for _ in media.read_access_units("clip.mp4", "H264", first_timeout=5, stall_timeout=0.2):
            pass
        return time.monotonic() - t0

    assert asyncio.run(run()) < 2 and Proc.killed


def test_cancel_stops_the_reader_even_while_data_keeps_coming(monkeypatch):
    """A seek cancels the running replay; it must not survive that and keep the RTSP session."""
    from bcproxy import media

    class Out:
        async def read(self, n):
            await asyncio.sleep(0)         # always ready: the race wait_for loses
            return b"\x00\x00\x00\x01\x65" + bytes(64)

    class Proc:
        stdout, stderr, returncode, pid = Out(), None, None, 1
        killed = False

        def kill(self):
            Proc.killed = True

        async def wait(self):
            return 0

    async def spawn(*a, **k):
        return Proc()

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)

    async def consume():
        async for _ in media.read_access_units("clip.mp4", "H264"):
            pass

    async def run():
        for _ in range(50):
            t = asyncio.create_task(consume())
            await asyncio.sleep(0.002)
            t.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(t), 1)
            except asyncio.CancelledError:
                pass
            assert t.done(), "reader survived the cancel"

    asyncio.run(run())
    assert Proc.killed


def test_replay_falls_back_to_the_codec_the_clip_was_recorded_with(monkeypatch):
    tried = []

    async def fake_aus(url, codec):
        tried.append(codec)
        if codec == "H265":        # ffmpeg: "hevc muxer supports only codec hevc"
            return
        yield AccessUnit(b"\x00\x00\x00\x01\x65" + bytes(100), True)
        yield AccessUnit(b"\x00\x00\x00\x01\x41" + bytes(100), False)

    monkeypatch.setattr(session, "read_access_units", fake_aus)

    class Main:
        info = StreamInfo("H265", 640, 360, 10)

        async def ensure_info(self):
            return self.info

    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw", listen_host="127.0.0.1"))
        cam.recordings = pb.HikRecordings(FakeHttp())
        cam.main = Main()
        cam.cfg.main_rtsp = "rtsp://u:p@h/Streaming/Channels/101"
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        r = await c.request(381, payload=bc.body_xml(
            '<ReplayByTimeV2 version="1.1">\n<seq>1793769300</seq>\n' + _t("startTime", 2026, 11, 1, 7, 0, 0)
            + _t("endTime", 2026, 11, 1, 8, 0, 0) + "<streamType>mainStream</streamType>\n</ReplayByTimeV2>"))
        q = c.subs[(381, r.msg_num)]
        rd, kinds = BcMediaReader(), []
        while "I" not in kinds:
            m = await asyncio.wait_for(q.get(), 3)
            if m.binary:
                kinds += [x[0] for x in rd.feed(m.payload)]
        c.writer.close()
        srv.close()

    asyncio.run(run())
    assert tried[:2] == ["H265", "H264"]
