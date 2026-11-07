"""Motion boxes: the camera's target packets (against real samples), the frame extension, and the stream."""
import asyncio
import json
import struct
from pathlib import Path

import pytest

from bcproxy import bc, config, motionbox
from bcproxy.bc import ext_xml
from bcproxy.bcmedia import AccessUnit, BcMediaReader, iframe, pframe
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.media import StreamInfo
from bcproxy.motionbox import GRID_H, GRID_W, Box, CameraTargets, FrameEncoder, parse_targets, rtp_private_packet
from bcproxy.session import BcSession
from test_full_aes import bc_client

SAMPLES = Path(__file__).parent / "samples"
REAL = json.loads((SAMPLES / "hik_targets.json").read_text())
BOX = Box(10, 20, 110, 220, 7)


def _lz4_block(src: bytes) -> bytes:
    out, i = bytearray(), 0
    while i < len(src):
        token = src[i]
        i += 1
        n = token >> 4
        if n == 15:
            while True:
                i, more = i + 1, src[i]
                n += more
                if more != 255:
                    break
        out += src[i:i + n]
        i += n
        if i >= len(src):
            break
        offset = src[i] | src[i + 1] << 8
        i += 2
        m = token & 15
        if m == 15:
            while True:
                i, more = i + 1, src[i]
                m += more
                if more != 255:
                    break
        for _ in range(m + 4):
            out.append(out[-offset])
    return bytes(out)


def _tlvs(data: bytes) -> list[tuple[int, bytes]]:
    out, i = [], 0
    while i + 3 <= len(data) and data[i]:            # tag 0: the padding
        tag, n = data[i], struct.unpack_from("<H", data, i + 1)[0]
        out.append((tag, data[i + 3:i + 3 + n]))
        i += 3 + n
    assert data[i:] == bytes(len(data) - i)
    return out


def unpack(ext: bytes) -> tuple[int, bytes]:
    """(time of the frame, decompressed data) of an extension."""
    (tag, body), = _tlvs(ext)
    assert tag == 0xFF
    (t1, times), (t2, inner) = _tlvs(body)
    assert (t1, t2) == (1, 2)
    (_, micros), = _tlvs(times)
    (_, name), (_, frame) = _tlvs(inner)
    assert name == b"lz4 " and frame[:4] == b"\x04\x22\x4d\x18"
    i, out = 7, bytearray()
    while True:
        size = struct.unpack_from("<I", frame, i)[0]
        i += 4
        if size == 0:
            break
        out += _lz4_block(frame[i:i + size])
        i += size
    return struct.unpack("<Q", micros)[0], bytes(out)


def shape(section: bytes) -> list:
    """Of one section: its groups, each with the tag and size of what it holds."""
    return [(tag, [(t, len(v)) for t, v in _tlvs(group)]) for tag, group in _tlvs(section)]


# ---------------------------------------------------------------------------
# the camera's packets
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("sample", REAL["backdoor_recording"], ids=lambda s: f"{len(s['plain'])}targets")
def test_targets_match_the_plain_copy_in_the_same_recording(sample):
    boxes = parse_targets(bytes.fromhex(sample["packet"]))
    plain = sorted(sample["plain"])
    assert [b.track for b in sorted(boxes, key=lambda b: b.track)] == [p[0] for p in plain]
    for b, (_, x, y, w, h) in zip(sorted(boxes, key=lambda b: b.track), plain):
        assert b.x1 == pytest.approx(x * GRID_W, abs=1.5) and b.y1 == pytest.approx(y * GRID_H, abs=1.5)
        assert b.x2 == pytest.approx((x + w) * GRID_W, abs=1.5) and b.y2 == pytest.approx((y + h) * GRID_H, abs=1.5)


@pytest.mark.parametrize("count", ["1", "2", "3"])
def test_the_format_with_more_after_each_target(count):
    boxes = parse_targets(bytes.fromhex(REAL["mountainview_targets"][count]))
    assert len(boxes) == int(count)
    for b in boxes:
        assert 0 <= b.x1 < b.x2 <= GRID_W and 0 <= b.y1 < b.y2 <= GRID_H
    assert len({b.track for b in boxes}) == int(count)


@pytest.mark.parametrize("packet", REAL["not_targets"], ids=range(len(REAL["not_targets"])))
def test_other_private_packets_give_nothing(packet):
    assert parse_targets(bytes.fromhex(packet)) is None


@pytest.mark.parametrize("cut", [28, 30, 36, 40])
def test_a_cut_packet_gives_nothing(cut):
    assert parse_targets(bytes.fromhex(REAL["mountainview_targets"]["3"])[:cut]) is None


def test_an_unknown_kind_of_list_gives_nothing():
    packet = bytearray.fromhex(REAL["mountainview_targets"]["1"])
    packet[15] = 0x99
    assert parse_targets(bytes(packet)) is None


def test_coordinates_outside_the_picture_give_nothing():
    packet = bytearray.fromhex(REAL["mountainview_targets"]["1"])
    packet[24:28] = struct.pack(">HH", 0x8000 | 8, 0x8000 | 8)
    assert parse_targets(bytes(packet)) is None


def test_the_private_packet_of_an_rtp_packet():
    rtp = bytes.fromhex(REAL["rtp_packet"])
    assert rtp_private_packet(rtp)[:2] == b"\x00\x03"
    assert parse_targets(rtp_private_packet(rtp))
    assert rtp_private_packet(b"\x80" + rtp[1:]) is None      # no extension: video
    assert rtp_private_packet(b"\x90\x60") is None


# ---------------------------------------------------------------------------
# the Reolink frame extension
# ---------------------------------------------------------------------------

def test_a_frame_without_boxes_is_the_same_as_a_real_one():
    real = unpack((SAMPLES / "motionbox_real_empty.bin").read_bytes())[1]
    assert unpack(FrameEncoder().encode(5, ()))[1] == real


def test_a_frame_with_boxes_has_the_shape_of_a_real_one():
    real = unpack((SAMPLES / "motionbox_real_3boxes.bin").read_bytes())[1]
    boxes = (Box(0, 80, 282, 214, 1), Box(553, 8, 608, 55, 2), Box(299, 0, 378, 26, 3))
    ours = unpack(FrameEncoder().encode(5, boxes))[1]
    real_sections = dict(_tlvs(real))
    ours_sections = dict(_tlvs(ours))
    assert ours_sections[4] == real_sections[4]
    for section in (1, 2):           # section 3 holds the camera's class 0 only: one box in this sample
        assert shape(ours_sections[section]) == shape(real_sections[section])
    assert {len(v) for t, v in _tlvs(dict(_tlvs(ours_sections[3]))[2]) if t == 4} == \
           {len(v) for t, v in _tlvs(dict(_tlvs(real_sections[3]))[2]) if t == 4} == {13}


def test_box_values_and_times():
    enc = FrameEncoder()
    enc.encode(1000, ())
    micros, data = unpack(enc.encode(2000, (BOX,)))
    assert micros == 2000
    values = dict(_tlvs(dict(_tlvs(dict(_tlvs(data))[1]))[2]))
    assert struct.unpack("<Q", values[1])[0] == 1000      # the previous frame's, like a real camera
    assert struct.unpack("<Q", values[5])[0] == 2         # counts frames
    assert struct.unpack("<HH", values[3]) == (GRID_W, GRID_H)
    assert struct.unpack("<5H", values[4]) == (10, 20, 110, 220, motionbox.SCORE)
    detailed = dict(_tlvs(dict(_tlvs(dict(_tlvs(data))[2]))[2]))[4]
    assert struct.unpack("<HBB", detailed[10:]) == (7, 0, motionbox.CLASS)


def test_long_data_uses_the_length_extension():
    many = tuple(Box(i, i, i + 9, i + 9, i) for i in range(motionbox.MAX_BOXES))
    assert unpack(FrameEncoder().encode(5, many))[1] == motionbox._boxes_payload(many, 0, 1)
    payload = motionbox._lz4_frame(bytes(range(256)) * 3)
    assert _lz4_block(payload[11:-4]) == bytes(range(256)) * 3


def test_frames_with_extension_stay_readable_and_aligned():
    ext = FrameEncoder().encode(5, (BOX,))
    nal = b"\x00\x00\x00\x01\x26\x01\x02"
    for frame, kind in ((pframe("H265", nal, 5, ext=ext), "P"), (iframe("H265", nal, 5, 1793767134, ext=ext), "I")):
        assert len(frame) % 8 == 0
        (got, meta, data), = BcMediaReader().feed(frame)
        assert got == kind and data == nal
        extra = meta["extra"]
        if kind == "I":                     # the real I frame has its 8 bytes of time first
            assert struct.unpack("<I", extra[:4])[0] == 1793767134
            extra = extra[8:]
        assert unpack(extra)[0] == 5


def test_frames_without_extension_are_unchanged():
    nal = b"\x00\x00\x00\x01\x26\x01\x02"
    assert struct.unpack_from("<I", pframe("H265", nal, 5), 12)[0] == 0
    assert struct.unpack_from("<I", iframe("H265", nal, 5, 7), 12)[0] == 8


# ---------------------------------------------------------------------------
# the camera's stream
# ---------------------------------------------------------------------------

class FakeHttp:
    def __init__(self, status=200):
        self.status, self.asked = status, 0

    async def request(self, method, path, body=None):
        assert method == "GET" and path.startswith("/ISAPI/Security/token")
        self.asked += 1
        status = self.status

        class Response:
            def __init__(self):
                self.status = status

            async def read_all(self):
                return b'{"Token":{"value":"tok"}}'

            def close(self):
                pass
        return Response()


def _server_frame(data: bytes, opcode: int) -> bytes:
    n = len(data)
    head = bytes([0x80 | opcode]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n))
    return head + data


class FakeCamera:
    """The WebSocket of the web preview. refuse: streams answered with deviceError."""

    def __init__(self, packets, refuse=()):
        self.packets, self.refuse = packets, refuse
        self.paths, self.closed = [], asyncio.Event()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        path = head.split()[1].decode()
        self.paths.append(path)
        writer.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n\r\n")
        writer.write(_server_frame(b'{"version":"1.0","cipherSuite":"1"}', 1))
        await writer.drain()
        first = await reader.readexactly(2)
        size = first[1] & 0x7F
        if size == 126:
            size = struct.unpack(">H", await reader.readexactly(2))[0]
        mask = await reader.readexactly(4)
        sent = bytes(c ^ mask[i % 4] for i, c in enumerate(await reader.readexactly(size)))
        start = json.loads(sent)
        assert start["cmd"] == "realplay" and start["token"] == "tok" and start["url"].endswith(path.split("?")[0])
        if int(path.split("?")[0].rsplit("/", 1)[1]) in self.refuse:
            writer.write(_server_frame(b'{"statusCode":3,"subStatusCode":"deviceError"}', 1))
            await writer.drain()
            writer.close()
            return
        writer.write(_server_frame(b'{"statusCode":1,"sdp":"v=0"}', 1))
        for p in self.packets:
            writer.write(_server_frame(p, 2))
        await writer.drain()
        try:
            await reader.read()
        except ConnectionError:
            pass
        writer.close()
        self.closed.set()


async def until(check, seconds=5.0):
    for _ in range(int(seconds / 0.01)):
        if check():
            return True
        await asyncio.sleep(0.01)
    return False


def test_boxes_come_from_the_stream_and_only_while_watched():
    async def run():
        video = bytes.fromhex("8060") + bytes(30)
        targets = bytes.fromhex(REAL["rtp_packet"])
        async with FakeCamera([video, targets]) as cam:
            t = CameraTargets("t", FakeHttp(), "127.0.0.1", cam.port)
            assert not t.supported and t.boxes() == ()
            t.add_viewer()
            assert await until(lambda: t.supported)
            assert t.boxes() == parse_targets(rtp_private_packet(targets))
            assert cam.paths[0].startswith("/1/2?")       # the small stream first
            motionbox.LINGER_SECONDS, linger = 0, motionbox.LINGER_SECONDS
            try:
                t.remove_viewer()
                assert await until(cam.closed.is_set)     # nobody watches: disconnected
            finally:
                motionbox.LINGER_SECONDS = linger
    asyncio.run(run())


def test_a_camera_refusing_the_small_stream_is_asked_for_the_main_one():
    async def run():
        async with FakeCamera([bytes.fromhex(REAL["rtp_packet"])], refuse={2}) as cam:
            t = CameraTargets("t", FakeHttp(), "127.0.0.1", cam.port)
            t.add_viewer()
            assert await until(lambda: t.supported)
            assert [p.split("?")[0] for p in cam.paths] == ["/1/2", "/1/1"]
            t.stop()
    asyncio.run(run())


def test_a_camera_sending_no_targets_stays_unsupported():
    async def run():
        async with FakeCamera([bytes.fromhex("8060") + bytes(30)]) as cam:
            t = CameraTargets("t", FakeHttp(), "127.0.0.1", cam.port)
            t.add_viewer()
            assert await until(lambda: bool(cam.paths))
            await asyncio.sleep(0.1)
            assert not t.supported
            t.stop()
    asyncio.run(run())


def test_a_camera_without_tokens_is_asked_again_only_rarely(monkeypatch):
    async def run():
        http = FakeHttp(403)
        sleeps = []
        real_sleep = asyncio.sleep

        async def fake_sleep(seconds):
            sleeps.append(seconds)
            await real_sleep(0)
            if len(sleeps) > 2:
                raise asyncio.CancelledError
        monkeypatch.setattr(motionbox.asyncio, "sleep", fake_sleep)
        t = CameraTargets("t", http, "127.0.0.1", 1)
        with pytest.raises(asyncio.CancelledError):
            await t._run()
        assert sleeps and min(sleeps) == motionbox.RETRY_SECONDS
        assert not t.supported

    asyncio.run(run())


def test_old_boxes_are_not_shown():
    t = CameraTargets("t", None, "h")
    t._publish((BOX,))
    assert t.boxes() == (BOX,)
    t._fresh -= motionbox.STALE_SECONDS + 1
    assert t.boxes() == ()
    assert t.supported              # it did send some


# ---------------------------------------------------------------------------
# preview and config
# ---------------------------------------------------------------------------

class FakeSrc:
    def __init__(self):
        self.info = StreamInfo("H264", 640, 360, 15)
        self.q = asyncio.Queue()

    async def ensure_info(self):
        return self.info

    def subscribe(self):
        for i in range(3):
            self.q.put_nowait((i / 15, AccessUnit(b"\x00\x00\x00\x01\x65" + bytes(50) if i == 0
                                                  else b"\x00\x00\x00\x01\x41" + bytes(50), i == 0)))
        return self.q

    def unsubscribe(self, q):
        pass


def preview_frames(enabled: bool, publish: bool):
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw", main_rtsp="rtsp://x/1", sub_rtsp="rtsp://x/2",
                                         hik_host="127.0.0.1", motion_boxes=enabled))
        cam.sub = FakeSrc()
        boxes = cam.motion_boxes
        if boxes:
            async def no_stream():
                await asyncio.Event().wait()
            boxes._run = no_stream
            if publish:
                boxes._publish((BOX,))
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = bc_client.Client("127.0.0.1", port, "admin", "pw", offer=0x12)
        await c.connect()
        await c.login()
        r = await c.request(3, ext=ext_xml(channelId=0), payload=bc.body_xml(
            '<Preview version="1.1">\n<channelId>0</channelId>\n<handle>0</handle>\n'
            "<streamType>subStream</streamType>\n</Preview>"))
        q = c.subs[(3, r.msg_num)]
        rd = BcMediaReader()
        frames = [f for f in rd.feed(r.payload) if f[0] in "IP"]
        while len(frames) < 3:
            m = await asyncio.wait_for(q.get(), 3)
            frames += [f for f in rd.feed(m.payload) if f[0] in "IP"]
        viewers = boxes.viewers if boxes else None
        c.writer.close()
        srv.close()
        if boxes:
            await until(lambda: boxes.viewers == 0)
        after = boxes.viewers if boxes else None
        if boxes:
            boxes.stop()
        return frames, viewers, after

    return asyncio.run(run())


def test_preview_frames_carry_the_cameras_boxes():
    frames, viewers, after = preview_frames(True, True)
    assert viewers == 1 and after == 0
    assert [f[0] for f in frames] == ["I", "P", "P"]
    for kind, meta, _ in frames:
        extra = meta["extra"][8:] if kind == "I" else meta["extra"]
        fields = dict(_tlvs(dict(_tlvs(unpack(extra)[1]))[1]))
        assert struct.unpack("<HH", dict(_tlvs(fields[2]))[3]) == (GRID_W, GRID_H)
    assert struct.unpack("<5H", dict(_tlvs(dict(_tlvs(dict(_tlvs(unpack(frames[1][1]["extra"])[1]))[1]))[2]))[4]) \
        == (10, 20, 110, 220, motionbox.SCORE)


def test_no_extension_until_the_camera_has_sent_targets():
    frames, viewers, _ = preview_frames(True, False)
    assert viewers == 1             # it is listening
    assert len(frames[0][1]["extra"]) == 8 and frames[1][1]["extra"] == b""


def test_no_extension_when_turned_off():
    frames, viewers, _ = preview_frames(False, False)
    assert viewers is None
    assert len(frames[0][1]["extra"]) == 8 and frames[1][1]["extra"] == b""


def test_dahua_has_no_target_stream():
    cam = VirtualCamera(CameraConfig(name="t", vendor="dahua", hik_host="h", main_rtsp="rtsp://x/1"))
    assert cam.motion_boxes is None


@pytest.mark.parametrize("text, expected", [("", True), ("motion_boxes: false", False), ("motion_boxes: true", True),
                                            ("motion_boxes: off", False)])
def test_config(tmp_path, text, expected):
    f = tmp_path / "c.yaml"
    f.write_text(f"cameras:\n  - name: a\n    hik:\n      host: h\n      {text}\n", encoding="utf-8")
    assert config.load(str(f)).cameras[0].motion_boxes is expected
