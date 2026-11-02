"""Settings written by NVR/app are persisted and returned by the matching get (rename etc.)."""
import asyncio
import importlib.util
from pathlib import Path

from bcproxy import settings as st
from bcproxy.bc import body_xml, ext_xml, xml_value
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.session import BcSession

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

OSD_SET = body_xml('<OsdChannelName version="1.1">\n<channelId>0</channelId>\n<name>前院 &amp; 车道</name>\n'
                   '<enable>1</enable>\n</OsdChannelName>')


async def _session(cam, fn):
    srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
    c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
    await c.connect()
    await c.login()
    try:
        return await fn(c)
    finally:
        c.writer.close()
        srv.close()


def test_rename_via_osd_shows_everywhere_and_persists(tmp_path):
    cam = VirtualCamera(CameraConfig(name="driveway", password="pw", sync_name_to_hik=False),
                        state_dir=str(tmp_path))

    async def go(c):
        w = await c.request(45, ext=ext_xml(channelId=0), payload=OSD_SET)
        r44 = await c.request(44, ext=ext_xml(channelId=0))
        r80 = await c.request(80)
        r104 = await c.request(104)
        return w, r44, r80, r104

    w, r44, r80, r104 = asyncio.run(_session(cam, go))
    assert w.response == 200
    assert "<name>前院 &amp; 车道</name>" in r44.payload.decode()
    assert b"<OsdDatetime" in r44.payload               # rest of the OSD doc kept
    assert xml_value(r80.payload, "name") == "前院 &amp; 车道"
    assert xml_value(r104.payload, "deviceName") == "前院 &amp; 车道"
    assert (tmp_path / "driveway" / "name.txt").read_text(encoding="utf-8") == "前院 & 车道"

    # restart: new camera object, same state dir
    cam2 = VirtualCamera(CameraConfig(name="driveway", password="pw"), state_dir=str(tmp_path))
    assert cam2.display_name == "前院 & 车道"


def test_rename_via_general_105_updates_osd(tmp_path):
    cam = VirtualCamera(CameraConfig(name="d", password="pw", sync_name_to_hik=False), state_dir=str(tmp_path))

    async def go(c):
        await c.request(45, ext=ext_xml(channelId=0), payload=OSD_SET)   # stored 44 doc
        w = await c.request(105, payload=body_xml(
            '<SystemGeneral version="1.1">\n<deviceName>Garage</deviceName>\n</SystemGeneral>'))
        return w, await c.request(44, ext=ext_xml(channelId=0))

    w, r44 = asyncio.run(_session(cam, go))
    assert w.response == 200 and b"<name>Garage</name>" in r44.payload


def test_image_settings_merge(tmp_path):
    cam = VirtualCamera(CameraConfig(name="d", password="pw"), state_dir=str(tmp_path))

    async def go(c):
        await c.request(25, ext=ext_xml(channelId=0), payload=body_xml(
            '<VideoInput version="1.1">\n<channelId>0</channelId>\n<bright>200</bright>\n</VideoInput>'))
        return await c.request(26, ext=ext_xml(channelId=0))

    r = asyncio.run(_session(cam, go))
    assert xml_value(r.payload, "bright") == "200" and xml_value(r.payload, "contrast") == "128"


def test_fallback_pairing_requires_matching_element():
    doc = body_xml("<Foo>\n<a>1</a>\n</Foo>")
    assert st.get_for_set(81, body_xml("<Foo><a>2</a></Foo>"), lambda i: doc if i == 80 else None) == 80
    assert st.get_for_set(81, body_xml("<Bar><a>2</a></Bar>"), lambda i: doc if i == 80 else None) is None


def test_hik_name_sync(tmp_path):
    seen = {}

    async def run():
        async def hik(r, w):
            req = await r.readuntil(b"\r\n\r\n")
            line = req.split(b"\r\n")[0].decode()
            n = int(next((h.split(b":")[1] for h in req.split(b"\r\n") if h.lower().startswith(b"content-length")), 0))
            body = await r.readexactly(n) if n else b""
            if line.startswith("GET"):
                out = (b'<?xml version="1.0" encoding="UTF-8"?>\n<VideoInputChannel><id>1</id>'
                       b"<inputPort>1</inputPort><name>Camera 01</name></VideoInputChannel>")
            else:
                seen["put"] = (line, body)
                out = b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>"
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(out) + out)
            await w.drain()
            w.close()
        srv = await asyncio.start_server(hik, "127.0.0.1", 0)
        cam = VirtualCamera(CameraConfig(name="d", password="pw", hik_host="127.0.0.1",
                                         hik_port=srv.sockets[0].getsockname()[1]), state_dir=str(tmp_path))
        assert cam.rename("车道")
        await asyncio.gather(*cam._tasks)
        srv.close()

    asyncio.run(run())
    line, body = seen["put"]
    assert line.startswith("PUT /ISAPI/System/Video/inputs/channels/1 ")
    assert "<name>车道</name>" in body.decode() and b"<inputPort>1</inputPort>" in body
