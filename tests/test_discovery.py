import asyncio
import socket

from bcproxy import discovery
from bcproxy.camera import CameraConfig, VirtualCamera

# Reply of a real RLC camera (UID set), with MAC/UID/name/IP replaced.
REAL_IPC = bytes.fromhex(
    "00" * 50 + "0102010001000001" + "00" * 22 + "ec71db123456" + "00" * 18 + "aaaa0000"
    + "3139322e3136382e312e3230" + "00" * 8 + "28230000"
    + "4672 6f6e 7459 6172 64".replace(" ", "") + "00" * 23
    + "45433a37313a44423a31323a33343a3536" + "00" * 47
    + "39353237303030414243444546474849" + "00" * 48 + "00" * 96)


def test_build_matches_real_layout():
    assert len(REAL_IPC) == discovery.REPLY_LEN
    got = discovery.build_reply("192.168.1.20", 9000, "FrontYard", "ec:71:db:12:34:56", "9527000ABCDEFGHI")
    assert got == REAL_IPC


def test_parse_roundtrip_and_truncation():
    name = "前门摄像头" * 4   # 60 UTF-8 bytes, field holds 31
    d = discovery.parse_reply(discovery.build_reply("10.0.0.5", 9001, name, "02-00-00-AA-BB-CC"))
    assert d["ip"] == "10.0.0.5" and d["port"] == 9001 and d["type"] == "IPC"
    assert d["mac"] == "02:00:00:AA:BB:CC" and d["uid"] == ""
    assert name.startswith(d["name"]) and len(d["name"].encode()) <= 31


def test_default_mac_stable():
    assert discovery.default_mac("driveway") == discovery.default_mac("driveway")
    assert discovery.default_mac("driveway").startswith("EC:71:DB:")
    assert discovery.default_mac("a") != discovery.default_mac("b")


def test_responder_answers_per_camera():
    async def run():
        cams = [VirtualCamera(CameraConfig(name="a", listen_host="127.0.0.1", uid="9527000AAAAAAAAA")),
                VirtualCamera(CameraConfig(name="b", listen_host="0.0.0.0", listen_port=9100,
                                           display_name="Back", mac="02:11:22:33:44:55"))]
        cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # stands in for UDP 3000
        cli.bind(("127.0.0.1", 0))
        cli.setblocking(False)
        srv = await discovery.serve(cams, "127.0.0.1", 0, reply_port=cli.getsockname()[1])
        port = srv.get_extra_info("sockname")[1]
        loop = asyncio.get_running_loop()
        asker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)   # replies must not go to the source port
        asker.sendto(b"\x01\x02\x03\x04", ("127.0.0.1", port))   # junk: no reply
        asker.sendto(discovery.REQUEST, ("127.0.0.1", port))
        got = [discovery.parse_reply(await asyncio.wait_for(loop.sock_recv(cli, 4096), 2)) for _ in cams]
        srv.close()
        cli.close()
        asker.close()
        return got

    a, b = asyncio.run(run())
    assert (a["ip"], a["port"], a["name"], a["uid"]) == ("127.0.0.1", 9000, "a", "9527000AAAAAAAAA")
    assert a["mac"] == discovery.default_mac("a")
    assert (b["ip"], b["port"], b["name"], b["mac"]) == ("127.0.0.1", 9100, "Back", "02:11:22:33:44:55")
