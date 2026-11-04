"""Full AES (0x12): binary payloads (video, JPEG) are encrypted, <encryptLen> in the extension."""
import asyncio
import importlib.util
from pathlib import Path

from bcproxy import bc
from bcproxy.bc import BcMsg, ext_xml, xml_value
from bcproxy.bcmedia import AccessUnit
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.crypto import Crypter, Enc, aes_decrypt, aes_encrypt, make_aes_key
from bcproxy.media import StreamInfo
from bcproxy.session import BcSession

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

KEY = make_aes_key("0123456789ABCDEF", "pw")


def test_encode_binary_full_aes_roundtrip():
    tx, rx = Crypter(), Crypter()
    tx.set(Enc.FULL_AES, KEY)
    rx.set(Enc.FULL_AES, KEY)
    data = __import__('os').urandom(2560)
    first = bc.encode(BcMsg(3, 0, 0, 7, 200, bc.CLASS_MODERN, ext_xml(binaryData=1), data, True), tx)
    nxt = bc.encode(BcMsg(3, 0, 0, 7, 200, bc.CLASS_MODERN, b"", data[:100], True), tx)
    assert data not in first                       # payload really encrypted on the wire
    d = bc.Decoder(rx)
    d.feed(first + nxt)
    m1, m2 = list(d.messages())
    assert m1.binary and m1.payload == data
    assert xml_value(m1.ext, "binaryData") == "1" and xml_value(m1.ext, "encryptLen") == str(len(data))
    assert m2.binary and m2.payload == data[:100]
    assert xml_value(m2.ext, "encryptLen") == "100"


def test_encode_binary_plain_aes_unchanged():
    tx = Crypter()
    tx.set(Enc.AES, KEY)
    data = b"\x00\x01binary" * 50
    raw = bc.encode(BcMsg(3, 0, 0, 7, 200, bc.CLASS_MODERN, ext_xml(binaryData=1), data, True), tx)
    assert raw.endswith(data)
    ext_len = int.from_bytes(raw[20:24], "little")
    assert b"encryptLen" not in aes_decrypt(KEY, raw[24:24 + ext_len])


class FakeSrc:
    def __init__(self):
        self.info = StreamInfo("H264", 640, 360, 15)
        self.q = asyncio.Queue()

    async def ensure_info(self):
        return self.info

    def subscribe(self):
        for i in range(3):
            self.q.put_nowait((i / 15, AccessUnit(b"\x00\x00\x00\x01\x65" + bytes(5000) if i == 0
                                                  else b"\x00\x00\x00\x01\x41" + bytes(800), i == 0)))
        return self.q

    def unsubscribe(self, q):
        pass


def test_nvr_offer_0x12_negotiates_full_aes_and_streams():
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw"))
        cam.sub = FakeSrc()

        async def jpeg():
            return b"\xff\xd8" + bytes(70000) + b"\xff\xd9"
        cam.snapshot_jpeg = jpeg
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w, trace=True).run(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        c = bc_client.Client("127.0.0.1", port, "admin", "pw", offer=0x12)
        await c.connect()
        await c.login()
        assert c.neg == Enc.FULL_AES and c.crypter.mode == Enc.FULL_AES
        r = await c.request(3, ext=ext_xml(channelId=0), payload=bc.body_xml(
            '<Preview version="1.1">\n<channelId>0</channelId>\n<handle>0</handle>\n'
            "<streamType>subStream</streamType>\n</Preview>"))
        q = c.subs[(3, r.msg_num)]
        from bcproxy.bcmedia import BcMediaReader
        rd = BcMediaReader()
        kinds = [k for k, _, _ in rd.feed(r.payload)]
        while kinds.count("I") + kinds.count("P") < 3:
            m = await asyncio.wait_for(q.get(), 3)
            kinds += [k for k, _, _ in rd.feed(m.payload)]
        r = await c.request(109, ext=ext_xml(channelId=0), payload=b"")
        size = int(xml_value(r.payload, "pictureSize"))
        buf, sq = b"", c.subs[(109, r.msg_num)]
        while True:
            m = await asyncio.wait_for(sq.get(), 3)
            buf += m.payload
            if m.response == 201:
                break
        c.writer.close()
        srv.close()
        return kinds, size, buf

    kinds, size, buf = asyncio.run(run())
    assert kinds[0] == "info" and kinds.count("I") == 1 and kinds.count("P") == 2
    assert size == len(buf) and buf.startswith(b"\xff\xd8") and buf.endswith(b"\xff\xd9")


def test_config_aes_still_downgrades_when_asked():
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw", encryption="aes"))
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw", offer=0x12)
        await c.connect()
        await c.login()
        c.writer.close()
        srv.close()
        return c.neg
    assert asyncio.run(run()) == Enc.AES


def test_nego_class_0000_variant_still_logs_in():
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw", nego_class=0x0000))
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw", offer=0x12)
        await c.connect()
        await c.login()
        c.writer.close()
        srv.close()
        return c.neg
    assert asyncio.run(run()) == Enc.FULL_AES


def test_nvr_reverse_magic_is_echoed():
    """Reolink NVRs use magic 0x0fedcba0 and drop replies with 0x0abcdef0."""
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw"))
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        # raw: exact 20 bytes the NVR sent in the field
        r, w = await asyncio.open_connection("127.0.0.1", port)
        w.write(bytes.fromhex("a0cbed0f01000000000000000000009b12dc1465"))
        hdr = await asyncio.wait_for(r.readexactly(20), 3)
        w.close()
        # full login + request using the NVR magic
        c = bc_client.Client("127.0.0.1", port, "admin", "pw", offer=0x12, magic=bc.MAGIC_REV)
        await c.connect()
        await c.login()
        rep = await c.request(199)
        assert c.decoder.magic == bc.MAGIC_REV
        c.writer.close()
        srv.close()
        return hdr, rep

    hdr, rep = asyncio.run(run())
    assert hdr[:4] == bytes.fromhex("a0cbed0f")
    assert hdr[14:16] == bytes.fromhex("009b") and hdr[16:18] == bytes.fromhex("12dd")
    assert rep.response == 200 and b"<Support" in rep.payload


def test_binary_mode_is_per_request_id():
    """The app keeps num=0 and counts in the channel/stream bytes: a binary
    stream on one request must not turn the next request into binary."""
    tx, rx = Crypter(), Crypter()
    tx.set(Enc.FULL_AES, KEY)
    rx.set(Enc.FULL_AES, KEY)
    talk = bc.encode(BcMsg(202, 9, 0, 0, 0, bc.CLASS_MODERN_REQ, ext_xml(binaryData=1), b"\x01" * 50, True), tx)
    xml = b'<?xml version="1.0" encoding="UTF-8" ?>\n<body><X/></body>\n'
    nxt = bc.encode(BcMsg(14, 10, 0, 0, 0, bc.CLASS_MODERN_REQ, b"", xml), tx)
    d = bc.Decoder(rx)
    d.feed(talk + nxt)
    m1, m2 = list(d.messages())
    assert m1.binary and not m2.binary and m2.payload == xml


def test_replay_frame_header_carries_seq():
    """Replay data (msg 381) puts the session seq in the response/class bytes,
    still with a 24-byte header; real cameras spell it <EncryptLen>."""
    seq = 1793605341
    tx, rx = Crypter(), Crypter()
    tx.set(Enc.FULL_AES, KEY)
    rx.set(Enc.FULL_AES, KEY)
    data = b"00dcH264" + bytes(range(256)) * 8
    frame = bc.encode(BcMsg(381, 35, 0, 0, seq & 0xFFFF, seq >> 16, ext_xml(binaryData=1), data, True), tx)
    assert bc.has_offset(seq >> 16) and len(frame) > 24
    d = bc.Decoder(rx)
    d.feed(frame + frame)
    msgs = list(d.messages())
    assert len(msgs) == 2 and d.skipped == 0
    assert msgs[0].payload == data and (msgs[0].cls << 16 | msgs[0].response) == seq
    # capital E variant (as sent by real cameras)
    ext = ext_xml(binaryData=1).replace(b"</Extension>", b"<EncryptLen>16</EncryptLen>\n</Extension>")
    enc_ext = tx.encrypt(381, 0, ext)
    body = enc_ext + aes_encrypt(KEY, data[:16]) + data[16:]
    raw = bc.HDR.pack(bc.MAGIC, 381, len(body), 1, 0, 0, seq & 0xFFFF, seq >> 16) + len(enc_ext).to_bytes(4, "little")
    d2 = bc.Decoder(rx)
    d2.feed(raw + body)
    (m,) = list(d2.messages())
    assert m.binary and m.payload == data
