"""In-process: client -> recording relay -> proxy. Then decode the recorded pcap with bc_dump."""
import asyncio
import importlib.util
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "tools"))

from bcproxy.camera import CameraConfig, VirtualCamera  # noqa: E402
from bcproxy.session import BcSession  # noqa: E402


def _load(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / f"{name}.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


bc_client = _load("bc_client")
bc_dump = _load("bc_dump")


def write_pcap(path, chunks):
    """chunks: list of (from_client: bool, bytes). Fake Ethernet/IPv4/TCP."""
    seq = {True: 1000, False: 5000}
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    cli, srv = bytes([10, 0, 0, 2]), bytes([10, 0, 0, 9])
    for i, (from_cli, data) in enumerate(chunks):
        for off in range(0, len(data), 1400):
            seg = data[off:off + 1400]
            sport, dport = (40000, 9000) if from_cli else (9000, 40000)
            src, dst = (cli, srv) if from_cli else (srv, cli)
            tcp = struct.pack(">HHIIBBHHH", sport, dport, seq[from_cli], 0, 5 << 4, 0x18, 65535, 0, 0)
            ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp) + len(seg), 0, 0, 64, 6, 0, src, dst)
            eth = b"\x00" * 12 + b"\x08\x00"
            pkt = eth + ip + tcp + seg
            out += struct.pack("<IIII", i, 0, len(pkt), len(pkt)) + pkt
            seq[from_cli] += len(seg)
    Path(path).write_bytes(out)


def test_session_and_pcap_dump(tmp_path):
    async def run():
        cam = VirtualCamera(CameraConfig(name="t", password="pw",
                                         smart_zones={"crossline": [{"zone": 0, "name": "L"}]}))
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        sport = srv.sockets[0].getsockname()[1]
        chunks = []

        async def relay(r, w):
            ur, uw = await asyncio.open_connection("127.0.0.1", sport)

            async def pump(src, dst, from_cli):
                while data := await src.read(65536):
                    chunks.append((from_cli, data))
                    dst.write(data)
                    await dst.drain()
                dst.close()
            await asyncio.gather(pump(r, uw, True), pump(ur, w, False), return_exceptions=True)

        rel = await asyncio.start_server(relay, "127.0.0.1", 0)
        rport = rel.sockets[0].getsockname()[1]
        c = bc_client.Client("127.0.0.1", rport, "admin", "pw")
        await c.connect()
        await c.login()
        r199 = await c.request(199)
        r527 = await c.request(527)
        r529 = await c.request(529)
        await c.request(31)
        from bcproxy.events import DetEvent
        cam.state.apply(DetEvent("crossline", True, 0, frozenset({"people"})))
        cam._maybe_push()
        first = await asyncio.wait_for(c.events.get(), 3)      # idle state pushed on subscribe
        assert b"<status>none</status>" in first.payload
        ev = await asyncio.wait_for(c.events.get(), 3)
        c.writer.close()
        await asyncio.sleep(0.1)
        srv.close()
        rel.close()
        return r199, r527, r529, ev, chunks

    r199, r527, r529, ev, chunks = asyncio.run(run())
    assert b"<aitype>7</aitype>" in r199.payload and b"<smartAI>2</smartAI>" in r199.payload
    assert r527.response == 200 and b"<crosslineDetectItem>" in r527.payload
    assert r529.response == 400          # intrusion not configured
    assert b"<type>crossline</type>" in ev.payload and b"<AItype>people</AItype>" in ev.payload

    pcap = tmp_path / "s.pcap"
    write_pcap(pcap, chunks)
    flows = bc_dump.reassemble(bc_dump.tcp_segments(str(pcap), 9000))
    cam_bytes = flows[("10.0.0.9:9000", "10.0.0.2:40000")]
    nvr_bytes = flows[("10.0.0.2:40000", "10.0.0.9:9000")]
    cmsgs, nonce, neg = bc_dump.decode_camera(cam_bytes, "pw")
    nmsgs = bc_dump.decode_nvr(nvr_bytes, "pw", nonce, neg)
    assert neg.name == "FULL_AES"
    assert [m.msg_id for m in nmsgs] == [1, 1, 199, 527, 529, 31]
    assert any(m.msg_id == 199 and b"<Support" in m.payload for m in cmsgs)
    assert any(m.msg_id == 33 and b"crossline" in m.payload for m in cmsgs)
