"""<Performance> (bitrate / CPU shown in the NVR channel info)."""
import asyncio

from bcproxy.bc import xml_value
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.media import RateMeter


def test_probe_values_identify_msg_id():
    cam = VirtualCamera(CameraConfig(name="t", perf_probe=True))
    p112, p132 = cam.performance_xml(112), cam.performance_xml(132)
    assert xml_value(p112, "cpuUsed") == "12" and xml_value(p112, "codecRate") == "1112"
    assert xml_value(p132, "cpuUsed") == "32" and xml_value(p132, "codecRate") == "1132"


def test_rate_meter():
    m = RateMeter()
    assert m.kbps() is None
    for _ in range(10):
        m.add(50_000)                       # 500 KB within <1 s, span clamped to 1 s
    assert m.kbps() == 4000


def test_real_values_from_stream_and_hik(tmp_path):
    async def run():
        async def hik(r, w):
            req = await r.readuntil(b"\r\n\r\n")
            path = req.split()[1]
            if path == b"/ISAPI/System/status":
                out = b"<DeviceStatus><CPUList><CPU><cpuUtilization>23</cpuUtilization></CPU></CPUList></DeviceStatus>"
            else:
                out = (b"<StreamingChannel><Video><videoQualityControlType>VBR</videoQualityControlType>"
                       b"<constantBitRate>2048</constantBitRate><vbrUpperCap>4096</vbrUpperCap></Video></StreamingChannel>")
            w.write(b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(out) + out)
            await w.drain()
            w.close()
        srv = await asyncio.start_server(hik, "127.0.0.1", 0)
        cam = VirtualCamera(CameraConfig(name="t", hik_host="127.0.0.1", main_rtsp="rtsp://x/1",
                                         hik_port=srv.sockets[0].getsockname()[1]), state_dir=str(tmp_path))
        t = asyncio.create_task(cam._poll_status(interval=60))
        for _ in range(50):
            await asyncio.sleep(0.02)
            if cam.hik_bitrate is not None:
                break
        idle = cam.performance_xml(112)          # no live stream: configured bitrate
        cam.main.rate.add(500_000)               # live stream: measured
        live = cam.performance_xml(112)
        t.cancel()
        srv.close()
        return idle, live

    idle, live = asyncio.run(run())
    assert xml_value(idle, "cpuUsed") == "23" and xml_value(idle, "codecRate") == "4096"
    assert xml_value(live, "codecRate") == "4000"


def test_encoder_templates_use_codec_and_hik_bitrate():
    from bcproxy.media import StreamInfo
    cam = VirtualCamera(CameraConfig(name="t", main_rtsp="rtsp://x/1",
                                     main_info=StreamInfo("H265", 2688, 1520, 30),
                                     sub_info=StreamInfo("H264", 640, 360, 15)))
    cam.hik_bitrate, cam.hik_sub_bitrate = 6144, 768
    v = cam.template_vars()
    enc = cam.templates.render(56, v).decode()
    sil = cam.templates.render(146, v).decode()
    assert "<videoEncType>1</videoEncType>" in enc.split("<subStream>")[0]
    assert "<videoEncType>0</videoEncType>" in enc.split("<subStream>")[1]
    assert "<bitRate>6144</bitRate>" in enc and "<bitRate>768</bitRate>" in enc
    assert "<framerateTable>30,25,22,20,18,16,15,12,10,8,6,4,2</framerateTable>" in sil
    assert "<framerateTable>15,12,10,8,6,4,2</framerateTable>" in sil
    assert "6144" in xml_value(sil, "bitrateTable") and "${" not in sil + enc
