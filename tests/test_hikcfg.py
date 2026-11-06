"""Settings that must reach the Hikvision camera: streams, recording, light, audio."""
import asyncio
import importlib.util
import re
from pathlib import Path

from bcproxy import bc, bcmedia, hik, hikcfg
from bcproxy import playback as pb
from bcproxy.bc import ext_xml, xml_value
from bcproxy.bcmedia import AccessUnit, BcMediaReader
from bcproxy.camera import CameraConfig, VirtualCamera
from bcproxy.media import StreamInfo, split_adts
from bcproxy.session import BcSession

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'


def _caps(w, h):
    return (f'<StreamingChannel {NS}><Video><videoResolutionWidth opt="{w}">1</videoResolutionWidth>'
            f'<videoResolutionHeight opt="{h}">1</videoResolutionHeight><vbrUpperCap min="32" max="8192">1</vbrUpperCap>'
            '<maxFrameRate opt="3000,2500,1500,100,50">1</maxFrameRate></Video></StreamingChannel>').encode()


def _stream(w, h, audio="true"):
    return (f"<StreamingChannel {NS}><Video><videoCodecType>H.265</videoCodecType>"
            f"<videoResolutionWidth>{w}</videoResolutionWidth><videoResolutionHeight>{h}</videoResolutionHeight>"
            "<constantBitRate>4096</constantBitRate><vbrUpperCap>4096</vbrUpperCap><maxFrameRate>3000</maxFrameRate>"
            f"</Video><Audio><enabled>{audio}</enabled></Audio></StreamingChannel>").encode()


TRACK = (f"<Track {NS}><id>101</id><LoopEnable>true</LoopEnable><TrackSchedule><ScheduleBlockList>"
         '<ScheduleBlock ScheduleActionSize="8"><ScheduleBlockType>x</ScheduleBlockType>'
         + "".join(f"<ScheduleAction><id>1</id><ScheduleActionStartTime><DayOfWeek>{d}</DayOfWeek>"
                   "<TimeOfDay>00:00:00</TimeOfDay></ScheduleActionStartTime><ScheduleActionEndTime>"
                   f"<DayOfWeek>{d}</DayOfWeek><TimeOfDay>24:00:00</TimeOfDay></ScheduleActionEndTime>"
                   "<Actions><Record>true</Record><ActionRecordingMode>EDR</ActionRecordingMode></Actions>"
                   "</ScheduleAction>" for d in hikcfg.DAYS)
         + "</ScheduleBlock></ScheduleBlockList></TrackSchedule><CustomExtensionList><CustomExtension>"
         "<enableSchedule>true</enableSchedule><PreRecordTimeSeconds>5</PreRecordTimeSeconds>"
         "<PostRecordTimeSeconds>5</PostRecordTimeSeconds></CustomExtension></CustomExtensionList></Track>")


class FakeHik:
    def __init__(self):
        self.docs = {"/ISAPI/Streaming/channels/101": _stream(2688, 1520),
                     "/ISAPI/Streaming/channels/102": _stream(640, 360, "false"),
                     "/ISAPI/Streaming/channels/101/capabilities": _caps("1280,1920,2688", "720,1080,1520"),
                     "/ISAPI/Streaming/channels/102/capabilities": _caps("640,640,1280", "360,480,720"),
                     "/ISAPI/Image/channels/1/supplementLight":
                         f"<SupplementLight {NS}><supplementLightMode>close</supplementLightMode></SupplementLight>".encode(),
                     "/ISAPI/Image/channels/1/supplementLight/capabilities":
                         (f'<SupplementLight {NS}><supplementLightMode opt="colorVuWhiteLight,close">close'
                          "</supplementLightMode></SupplementLight>").encode(),
                     "/ISAPI/ContentMgmt/record/tracks/101": TRACK.encode()}
        self.puts = []

    async def get_bytes(self, path):
        if path not in self.docs:
            raise ConnectionError(f"GET {path} -> HTTP 404")
        return self.docs[path]

    async def put_bytes(self, path, body, method="PUT"):
        self.puts.append(path)
        self.docs[path] = body
        return 200, b"<ResponseStatus><statusCode>1</statusCode></ResponseStatus>"


def _cam(**kw):
    cam = VirtualCamera(CameraConfig(name="t", password="pw", listen_host="127.0.0.1", hik_host="h", main_rtsp="rtsp://h/101",
                                     sub_rtsp="rtsp://h/102", audio=True, **kw))
    fake = FakeHik()
    cam.http = fake
    cam.hikcfg.http = fake
    cam.main.info = StreamInfo("H265", 2688, 1520, 30)
    cam.sub.info = StreamInfo("H265", 640, 360, 15)
    cam.hik_bitrate, cam.hik_sub_bitrate = 4096, 512
    return cam, fake


def test_stream_list_offers_every_main_and_sub_size():
    cam, _ = _cam()
    asyncio.run(cam.hikcfg.load())
    assert cam.hikcfg.caps[2].sizes == [(1280, 720), (640, 480), (640, 360)]
    doc = cam.stream_info_list()
    assert doc.count("<StreamInfo>") == 3
    mains = set(re.findall(r"<type>mainStream</type>.*?<width>(\d+)</width>", doc, re.S))
    subs = set(re.findall(r"<type>subStream</type>.*?<width>(\d+)</width>\n<height>(\d+)", doc, re.S))
    assert mains == {"1280", "1920", "2688"} and subs == {("640", "360")}   # sub size is fixed, like real cameras
    assert "<defaultGop>2</defaultGop>" in doc
    assert "<framerateTable>30,25," in doc    # not limited to the current frame rate


def test_compression_write_reaches_hikvision():
    cam, fake = _cam()

    async def run():
        await cam.hikcfg.load()
        await cam.apply_compression(bc.body_xml(
            '<Compression version="1.1">\n<channelId>0</channelId>\n<mainStream>\n<width>2688</width>\n<height>1520</height>\n'
            "<frame>30</frame>\n<bitRate>4096</bitRate>\n</mainStream>\n<subStream>\n<width>1280</width>\n<height>720</height>\n"
            "<frame>10</frame>\n<bitRate>1024</bitRate>\n<videoEncType>0</videoEncType>\n</subStream>\n</Compression>"))

    asyncio.run(run())
    assert fake.puts == ["/ISAPI/Streaming/channels/102"]       # main unchanged: left alone
    doc = fake.docs["/ISAPI/Streaming/channels/102"].decode()
    assert "<videoResolutionWidth>1280<" in doc and "<videoResolutionHeight>720<" in doc
    assert "<maxFrameRate>1000<" in doc and "<vbrUpperCap>1024<" in doc
    assert "<videoCodecType>H.265<" in doc      # the app's videoEncType is not trusted
    assert (cam.sub.info.width, cam.sub.info.height, cam.sub.info.fps, cam.sub.info.codec) == (1280, 720, 10, "H265")
    assert cam.hik_sub_bitrate == 1024


def test_size_hikvision_does_not_offer_is_refused():
    cam, fake = _cam()

    async def run():
        await cam.hikcfg.load()
        await cam.hikcfg.set_stream(1, 999, 999, 30, 4096)

    try:
        asyncio.run(run())
    except ValueError as e:
        assert "999x999" in str(e)
    else:
        raise AssertionError("accepted")
    assert fake.puts == []


def test_schedule_roundtrip_and_limits():
    t = hikcfg.schedule_tables(TRACK)
    assert t["event"] == "1" * 168 and t["Normal"] == "0" * 168
    tables = {"MD": "0" * 168, "people": "1" * 6 + "0" * 162, "Normal": "0" * 6 + "1" * 18 + "0" * 144}
    doc = hikcfg.replace_schedule(TRACK, hikcfg.schedule_actions(tables))
    assert doc.count("<ScheduleAction>") == 2 and "ScheduleActionSize" in doc
    back = hikcfg.schedule_tables(doc)
    assert back["event"].startswith("1" * 6 + "0") and back["Normal"][6:24] == "1" * 18 and back["Normal"][24:] == "0" * 144
    assert "<DayOfWeek>Sunday</DayOfWeek><TimeOfDay>06:00:00" in doc
    zebra = {"MD": ("10" * 12) * 7}
    try:
        hikcfg.schedule_actions(zebra)
    except ValueError as e:
        assert "12 schedule blocks" in str(e)
    else:
        raise AssertionError("too many blocks accepted")


def test_split_adts_resyncs_and_waits_for_full_frames():
    frame = bytes([0xFF, 0xF1, 0x60, 0x40, 0x01, 0x20, 0xFC]) + bytes(2)   # length 9
    buf = bytearray(b"\x00\x01" + frame + frame[:4])
    assert split_adts(buf) == [frame]
    assert bytes(buf) == frame[:4]


def test_journal_types_survive_merging():
    import datetime as dt
    t = dt.datetime(2026, 11, 3)
    segs = [pb.Segment(t + dt.timedelta(seconds=30 * i), t + dt.timedelta(seconds=30 * i + 10),
                       "people" if i % 2 else "vehicle,intrusion") for i in range(40)]
    assert pb.join_types("md", "vehicle", "intrusion") == "vehicle,intrusion,md"
    assert pb.join_types("sched", "vehicle") == "vehicle"
    assert pb.join_types("md") == "md"
    kept = pb.merge_segments(segs, t, t + dt.timedelta(days=1), max_items=100)
    assert {s.rec_type for s in kept} == {"people", "vehicle,intrusion"} and len(kept) == 40   # no need to merge
    tight = pb.merge_segments(segs, t, t + dt.timedelta(days=1), max_items=5)
    assert len(tight) <= 5 and "people" in tight[0].rec_type and "vehicle" in tight[0].rec_type


class FakeAudio:
    def __init__(self):
        self.q = asyncio.Queue()
        self.q.put_nowait(bytes([0xFF, 0xF1, 0x60, 0x40, 0x01, 0x20, 0xFC, 0, 0]))
        self.unsubscribed = False

    def subscribe(self):
        return self.q

    def unsubscribe(self, q):
        self.unsubscribed = True


class FakeVideo:
    info = StreamInfo("H264", 640, 360, 15)

    def __init__(self):
        self.q = asyncio.Queue()
        for i in range(30):
            self.q.put_nowait((i * 0.1, AccessUnit(b"\x00\x00\x00\x01" + (b"\x65" if i == 0 else b"\x41") + bytes(200), i == 0)))

    async def ensure_info(self):
        return self.info

    def subscribe(self, old_key=True):
        return self.q

    def unsubscribe(self, q):
        pass


def test_app_flow_light_record_audio():
    cam, fake = _cam(ai_types=["people", "vehicle"], light_seconds=0)
    cam.sub, cam.audio = FakeVideo(), FakeAudio()
    cam.audio_on = True

    async def run():
        await cam.hikcfg.load()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        pushes = c.subs.setdefault((291, 0), asyncio.Queue())
        await c.connect()
        await c.login()
        out = {"199": (await c.request(199)).payload, "151": (await c.request(151)).payload,
               "146": (await c.request(146)).payload, "40": (await c.request(40)).payload,
               "97": (await c.request(97)).payload, "210": (await c.request(210)).payload}
        rec = await c.request(81)
        out["81"] = rec.payload
        out["54"] = (await c.request(54)).payload
        out["82"] = (await c.request(82, payload=bc.body_xml(
            '<Record version="1.1">\n<channelId>0</channelId>\n<enable>1</enable>\n<typeScheduleList>\n<item>\n'
            f'<type>people</type>\n<valueTable>{"1" * 168}</valueTable>\n</item>\n</typeScheduleList>\n</Record>'))).response
        out["55"] = (await c.request(55, payload=bc.body_xml(
            '<RecordCfg version="1.1">\n<recordDelayTime>15</recordDelayTime>\n<preRecordTime>10</preRecordTime>\n'
            "<cycle>0</cycle>\n</RecordCfg>"))).response
        out["81_after"] = (await c.request(81)).payload
        out["light"] = (await c.request(288, payload=bc.body_xml(
            '<FloodlightManual version="1.1">\n<channelId>0</channelId>\n<status>1</status>\n<duration>180</duration>\n'
            "</FloodlightManual>"))).response
        out["push"] = [(await asyncio.wait_for(pushes.get(), 3)).payload for _ in range(2)]
        r = await c.request(3, ext=ext_xml(channelId=0), payload=bc.body_xml(
            '<Preview version="1.1">\n<channelId>0</channelId>\n<handle>0</handle>\n<streamType>subStream</streamType>\n</Preview>'))
        q, rd, kinds = c.subs[(3, r.msg_num)], BcMediaReader(), []
        while "audio" not in kinds:
            kinds += [k for k, _, _ in rd.feed((await asyncio.wait_for(q.get(), 3)).payload)]
        c.writer.close()
        srv.close()
        return out, kinds

    out, kinds = asyncio.run(run())
    assert b"<noAudio>0</noAudio>" in out["199"] and b"<lightType>1</lightType>" in out["199"]
    assert b"<binoCfg>0</binoCfg>" in out["199"]      # single lens: no "Sync Brightness" of the Duo series
    isp = int(xml_value(out["199"], "newIspCfg"))
    assert isp & (1 << 8) and isp & (1 << 12) and not isp & (1 << 2)    # brightness, sharpen on; exposure (not wired) off
    # ledState is the LED/IR control: it would add an IR button next to the white light
    assert b"ledState" not in out["151"] and b"recordSchedule_rw" in out["151"]
    assert out["146"].count(b"<StreamInfo>") >= 3      # FakeVideo info: sub sizes + its own size
    for k in ("40", "97", "210"):
        assert out[k]
    assert xml_value(out["81"], "enable") == "1" and b"<type>Normal</type>" in out["81"]
    assert xml_value(out["54"], "recordDelayTime") == "5" and xml_value(out["54"], "cycle") == "1"
    assert out["82"] == 200 and out["55"] == 200 and out["light"] == 200
    track = fake.docs["/ISAPI/ContentMgmt/record/tracks/101"].decode()
    assert "<PostRecordTimeSeconds>10<" in track and "<PreRecordTimeSeconds>10<" in track
    assert "<LoopEnable>false<" in track and track.count("<ScheduleAction>") == 7
    assert b"<type>people</type>" in out["81_after"]    # what the user chose is what the page shows
    assert [xml_value(p, "status") for p in out["push"]] == ["1", "0"]    # light_seconds=0: off right away
    light = fake.docs["/ISAPI/Image/channels/1/supplementLight"].decode()
    assert "<supplementLightMode>close<" in light
    assert "/ISAPI/Image/channels/1/supplementLight" in fake.puts and fake.puts.count("/ISAPI/Image/channels/1/supplementLight") == 2
    assert "audio" in kinds
    assert bcmedia.aac(b"x" * 9)[:4] == b"05wb"


IMAGE = {
    "Color": "<Color><brightnessLevel>50</brightnessLevel><contrastLevel>50</contrastLevel><saturationLevel>50</saturationLevel></Color>",
    "Sharpness": "<Sharpness><SharpnessLevel>50</SharpnessLevel></Sharpness>",
    "ImageFlip": "<ImageFlip><enabled>false</enabled></ImageFlip>",
    "powerLineFrequency": "<powerLineFrequency><powerLineFrequencyMode>60hz</powerLineFrequencyMode></powerLineFrequency>",
    "IrcutFilter": "<IrcutFilter><IrcutFilterType>auto</IrcutFilterType></IrcutFilter>",
    "WhiteBalance": "<WhiteBalance><WhiteBalanceStyle>naturallight</WhiteBalanceStyle><WhiteBalanceRed>50</WhiteBalanceRed>"
                    "<WhiteBalanceBlue>50</WhiteBalanceBlue></WhiteBalance>",
    "NoiseReduce": "<NoiseReduce><mode>general</mode></NoiseReduce>",
}


def _image_cam():
    cam, fake = _cam()
    for k, v in IMAGE.items():
        fake.docs[f"/ISAPI/Image/channels/1/{k}"] = v.encode()
    return cam, fake


def test_image_read_uses_hikvision_values():
    cam, fake = _image_cam()
    fake.docs["/ISAPI/Image/channels/1/Color"] = IMAGE["Color"].replace(">50</brightnessLevel>", ">40</brightnessLevel>").encode()
    fake.docs["/ISAPI/Image/channels/1/ImageFlip"] = b"<ImageFlip><enabled>true</enabled><ImageFlipStyle>UPDOWN</ImageFlipStyle></ImageFlip>"
    template = cam.templates.render(132, cam.template_vars())
    doc = asyncio.run(cam.hikcfg.image_xml(template))
    assert xml_value(doc, "bright") == "102" and xml_value(doc, "contrast") == "128"
    assert xml_value(doc, "flip") == "1" and xml_value(doc, "mirror") == "0"
    assert b"<shutterLevel>1/30</shutterLevel>" in doc and b"<Gainctl>" in doc     # layout kept
    assert b"<mode>60hz</mode>" in doc


def test_image_write_changes_only_what_differs():
    cam, fake = _image_cam()
    # as sent by the app: bogus shutter/power values must not reach Hikvision
    payload = bc.body_xml(
        '<VideoInput version="1.1">\n<channelId>0</channelId>\n<bright>104</bright>\n<contrast>128</contrast>\n'
        "<saturation>128</saturation>\n<hue>128</hue>\n<sharpen>128</sharpen>\n</VideoInput>\n"
        '<InputAdvanceCfg version="1.1">\n<PowerLineFrequency>\n<mode>outdoor</mode>\n</PowerLineFrequency>\n'
        "<Exposure>\n<shutterLevel>1/3</shutterLevel>\n</Exposure>\n<DayNight>\n<mode>color</mode>\n</DayNight>\n"
        "<mirror>1</mirror>\n<flip>1</flip>\n<nr3d>\n<enable>1</enable>\n</nr3d>\n<brightSync>0</brightSync>\n</InputAdvanceCfg>")
    assert asyncio.run(cam.hikcfg.apply_image(payload)) == ["Color", "ImageFlip", "IrcutFilter"]
    assert b"<brightnessLevel>41<" in fake.docs["/ISAPI/Image/channels/1/Color"]
    assert b"<ImageFlipStyle>CENTER<" in fake.docs["/ISAPI/Image/channels/1/ImageFlip"]
    assert b"<IrcutFilterType>day<" in fake.docs["/ISAPI/Image/channels/1/IrcutFilter"]
    assert asyncio.run(cam.hikcfg.apply_image(payload)) == []    # second save: nothing to do


ACCESS = ("<AdminAccessProtocolList>"
          "<AdminAccessProtocol version=\"2.0\"><id>1</id><enabled>true</enabled><protocol>HTTP</protocol><portNo>80</portNo></AdminAccessProtocol>"
          "<AdminAccessProtocol version=\"2.0\"><id>2</id><enabled>true</enabled><protocol>HTTPS</protocol><portNo>443</portNo></AdminAccessProtocol>"
          "<AdminAccessProtocol version=\"2.0\"><id>4</id><enabled>true</enabled><protocol>RTSP</protocol><portNo>554</portNo></AdminAccessProtocol>"
          "</AdminAccessProtocolList>")
INTEGRATE = "<Integrate><ONVIF><enable>true</enable></ONVIF><ISAPI><enable>true</enable></ISAPI></Integrate>"


def _ports(http=(1, 80), https=(1, 443), rtsp=(1, 554), onvif=1, server=9000):
    row = lambda t, p, v: f'<{t} version="1.1">\n<{p}>{v[1]}</{p}>\n<enable>{v[0]}</enable>\n</{t}>\n'  # noqa: E731
    return bc.body_xml(row("ServerPort", "serverPort", (1, server)) + row("HttpPort", "httpPort", http)
                       + row("HttpsPort", "httpsPort", https) + row("RtspPort", "rtspPort", rtsp)
                       + row("OnvifPort", "onvifPort", (onvif, 80)))


def test_server_ports_reflect_and_reach_hikvision():
    cam, fake = _cam()
    fake.docs["/ISAPI/Security/adminAccesses"] = ACCESS.encode()
    fake.docs["/ISAPI/System/Network/Integrate"] = INTEGRATE.encode()
    cfg = cam.hikcfg

    async def run():
        doc = await cfg.server_ports_xml(9000)
        assert "<httpPort>80</httpPort>\n<enable>1</enable>" in doc and "<rtspPort>554</rtspPort>\n<enable>1</enable>" in doc
        assert "<onvifPort>80</onvifPort>\n<enable>1</enable>" in doc
        assert await cfg.set_server_ports(_ports(), 9000) == []
        assert await cfg.set_server_ports(_ports(https=(0, 8443), onvif=0), 9000) == ["HTTPS", "ONVIF"]
        for bad in (_ports(http=(0, 80)), _ports(rtsp=(1, 8554)), _ports(server=9100)):
            try:
                await cfg.set_server_ports(bad, 9000)
            except ValueError:
                continue
            raise AssertionError("change that cuts the proxy off was accepted")

    asyncio.run(run())
    access = fake.docs["/ISAPI/Security/adminAccesses"].decode()
    assert "<enabled>false</enabled><protocol>HTTPS</protocol><portNo>8443<" in access
    assert "<enabled>true</enabled><protocol>HTTP</protocol><portNo>80<" in access
    assert b"<ONVIF><enable>false<" in fake.docs["/ISAPI/System/Network/Integrate"]


def test_bitrate_mode_comes_from_the_image_page():
    """Captured on a real Duo 2: the switch sends 25 with encType, never 57."""
    cam, fake = _image_cam()
    for path in ("/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102"):
        fake.docs[path] = fake.docs[path].replace(
            b"<constantBitRate>", b"<videoQualityControlType>VBR</videoQualityControlType><constantBitRate>")
    page = bc.body_xml('<InputAdvanceCfg version="1.1">\n<encType>{}</encType>\n</InputAdvanceCfg>')

    async def run():
        await cam.hikcfg.load()
        template = cam.templates.render(132, cam.template_vars())
        before = await cam.hikcfg.image_xml(template)
        changed = await cam.hikcfg.apply_image(page.replace(b"{}", b"1"))
        after = await cam.hikcfg.image_xml(template)
        # a later stream save always says encoderType=vbr: it must not undo the choice
        await cam.apply_compression(bc.body_xml(
            '<Compression version="1.1">\n<mainStream>\n<width>1920</width>\n<height>1080</height>\n<frame>30</frame>\n'
            "<bitRate>4096</bitRate>\n<encoderType>vbr</encoderType>\n</mainStream>\n</Compression>"))
        return before, changed, after

    before, changed, after = asyncio.run(run())
    assert xml_value(before, "encType") == "0" and xml_value(after, "encType") == "1"
    assert changed == ["bitrate mode cbr"]
    for path in ("/ISAPI/Streaming/channels/101", "/ISAPI/Streaming/channels/102"):
        assert b"<videoQualityControlType>CBR<" in fake.docs[path]


def test_pages_get_data_not_an_empty_ok():
    """The app refuses to open a page when one of its reads comes back empty."""
    cam, _ = _cam()

    async def run():
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        out = {}
        for i in (110, 296, 264, 267, 260, 232, 208, 289, 40, 97, 210, 455, 195, 42, 46, 76, 68, 70, 106, 217, 439):
            out[i] = await c.request(i)
        c.writer.close()
        srv.close()
        return out

    for i, r in asyncio.run(run()).items():
        assert r.response == 200 and b"<?xml" in r.payload, i


def test_frame_rate_mode_is_remembered_and_leaves_bitrate_mode_alone():
    """Saving the image page (constantFrameRate=0) must not undo CBR chosen on the stream page."""
    cam, fake = _image_cam()
    fake.docs["/ISAPI/Streaming/channels/101"] = fake.docs["/ISAPI/Streaming/channels/101"].replace(
        b"<constantBitRate>", b"<videoQualityControlType>CBR</videoQualityControlType><constantBitRate>")

    async def run():
        await cam.hikcfg.load()
        template = cam.templates.render(132, cam.template_vars())
        assert xml_value(await cam.hikcfg.image_xml(template, cam.frame_rate_mode), "constantFrameRate") == "0"
        cam.set_frame_rate_mode(1)
        assert xml_value(await cam.hikcfg.image_xml(template, cam.frame_rate_mode), "constantFrameRate") == "1"

    asyncio.run(run())
    assert b"<videoQualityControlType>CBR<" in fake.docs["/ISAPI/Streaming/channels/101"]
    assert cam.template_vars()["main_mode"] == "cbr"


def test_hdr_maps_to_hikvision_wdr():
    cam, fake = _image_cam()
    fake.docs["/ISAPI/Image/channels/1/WDR"] = b"<WDR><mode>open</mode><WDRLevel>50</WDRLevel></WDR>"
    template = cam.templates.render(132, cam.template_vars())
    doc = asyncio.run(cam.hikcfg.image_xml(template))
    assert xml_value(doc, "hdrSwitch") == "2"
    # the app echoes the page back: HDR off reaches Hikvision, the rest stays
    assert asyncio.run(cam.hikcfg.apply_image(doc.replace(b"<hdrSwitch>2<", b"<hdrSwitch>0<"))) == ["WDR"]
    assert b"<mode>close<" in fake.docs["/ISAPI/Image/channels/1/WDR"]
    del fake.docs["/ISAPI/Image/channels/1/WDR"]        # model without WDR: no HDR offered
    assert b"hdrSwitch" not in asyncio.run(cam.hikcfg.image_xml(template))


def test_antiflicker_placeholder_does_not_reach_hikvision():
    cam, fake = _image_cam()
    page = bc.body_xml('<InputAdvanceCfg version="1.1">\n<PowerLineFrequency>\n<mode>50hz</mode>\n<enable>{}</enable>\n'
                       "</PowerLineFrequency>\n</InputAdvanceCfg>")
    assert asyncio.run(cam.hikcfg.apply_image(page.replace(b"{}", b"0"))) == []
    assert b">60hz<" in fake.docs["/ISAPI/Image/channels/1/powerLineFrequency"]
    assert asyncio.run(cam.hikcfg.apply_image(page.replace(b"{}", b"1"))) == ["powerLineFrequency"]


def test_hikvision_clamp_is_what_the_proxy_reports():
    cam, fake = _cam()
    real_put = fake.put_bytes

    async def clamp(path, body, method="PUT"):
        return await real_put(path, body.replace(b"<maxFrameRate>3000<", b"<maxFrameRate>2500<"), method)

    fake.put_bytes = clamp
    cam.main.info = StreamInfo("H265", 1920, 1080, 30)

    async def run():
        await cam.hikcfg.load()
        await cam.apply_compression(bc.body_xml(
            '<Compression version="1.1">\n<mainStream>\n<width>2688</width>\n<height>1520</height>\n<frame>30</frame>\n'
            "<bitRate>4096</bitRate>\n</mainStream>\n</Compression>"))

    asyncio.run(run())
    assert (cam.main.info.width, cam.main.info.fps) == (2688, 25)


def test_hikvision_down_at_startup_is_retried():
    cam, fake = _cam()
    cam.recordings = pb.HikRecordings(fake)
    fake.docs["/ISAPI/ContentMgmt/Storage"] = (
        f"<storage {NS}><hddList><hdd><id>1</id><capacity>100</capacity><freeSpace>50</freeSpace></hdd></hddList></storage>").encode()
    real_get = fake.get_bytes
    down = [True]

    async def flaky(path):
        if down[0]:
            raise asyncio.TimeoutError()
        return await real_get(path)

    fake.get_bytes = flaky

    async def run():
        first = await cam._load_hik()
        keeps_playback = cam.recordings is not None
        down[0] = False
        return first, keeps_playback, await cam._load_hik()

    first, keeps_playback, second = asyncio.run(run())
    assert first is False and keeps_playback and second is True
    assert cam.hikcfg.caps[1].sizes and cam.hikcfg.light_on_mode == "colorVuWhiteLight"


def test_tamper_alert():
    cam, fake = _cam()
    path = "/ISAPI/System/Video/inputs/channels/1/tamperDetection"
    fake.docs[path] = (b"<TamperDetection><enabled>true</enabled><TamperDetectionRegionList><TamperDetectionRegion>"
                       b"<enabled>true</enabled></TamperDetectionRegion></TamperDetectionRegionList></TamperDetection>")

    async def run():
        assert not cam.aitype_bits & (1 << 25)
        await cam.hikcfg.load()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        ai = (await c.request(199)).payload
        before = (await c.request(763)).payload
        ok = (await c.request(764, payload=bc.body_xml(
            '<tamperAlarmCfg version="1.1">\n<enable>0</enable>\n</tamperAlarmCfg>'))).response
        after = (await c.request(763)).payload
        c.writer.close()
        srv.close()
        return ai, before, ok, after

    ai, before, ok, after = asyncio.run(run())
    assert int(xml_value(ai, "aitype")) & (1 << 25)
    assert xml_value(before, "enable") == "1" and ok == 200 and xml_value(after, "enable") == "0"
    doc = fake.docs[path]
    assert doc.count(b"<enabled>false</enabled>") == 1 and b"<TamperDetectionRegion><enabled>true" in doc


def test_no_tamper_ability_without_hikvision_support():
    cam, _ = _cam()
    asyncio.run(cam.hikcfg.load())
    assert not cam.hikcfg.has_tamper and not cam.aitype_bits & (1 << 25)


def test_tamper_event_is_reported():
    from bcproxy import hik
    from bcproxy.events import alarm_event_xml
    cam, _ = _cam()
    cam.on_alert(hik.HikAlert("shelteralarm", "active", 1))
    assert "tamper" in alarm_event_xml(cam.state.snapshot())


EXTERNAL_CAPS = (b'<ExternalDevice><SupplementLight><enabled opt="true,false">false</enabled>'
                 b'<mode opt="schedule,auto">auto</mode></SupplementLight></ExternalDevice>')
EXTERNAL_DOC = (b"<ExternalDevice><SupplementLight><enabled>false</enabled><brightnessRegulateMode>auto"
                b"</brightnessRegulateMode><mode>auto</mode><Schedule><TimeRange><beginTime>18:00:00</beginTime>"
                b"<endTime>06:00:00</endTime></TimeRange></Schedule><brightness>15</brightness>"
                b"<maxBrightness>50</maxBrightness></SupplementLight></ExternalDevice>")


def test_light_in_external_device_when_supplement_light_is_missing():
    """DS-2CD2347G1-L: supplementLight answers 403, the switch is in /ISAPI/System/externalDevice."""
    cam, fake = _cam(light_seconds=60)
    del fake.docs["/ISAPI/Image/channels/1/supplementLight/capabilities"]
    fake.docs["/ISAPI/System/externalDevice/capabilities"] = EXTERNAL_CAPS
    fake.docs["/ISAPI/System/externalDevice"] = EXTERNAL_DOC

    async def run():
        await cam.hikcfg.load()
        seen = [await cam.hikcfg.light_mode()]
        await cam.set_light(True)
        seen.append(fake.docs["/ISAPI/System/externalDevice"])
        await cam.set_light(False)
        return seen

    off, on_doc = asyncio.run(run())
    assert cam.hikcfg.light_on_mode == "on" and off == "close"
    for part in (b"<enabled>true</enabled>", b"<mode>schedule</mode>", b"<beginTime>00:00:00</beginTime>",
                 b"<endTime>23:59:59</endTime>", b"<brightnessRegulateMode>manual<", b"<brightness>50</brightness>"):
        assert part in on_doc
    assert fake.docs["/ISAPI/System/externalDevice"] == EXTERNAL_DOC and not cam.light_status   # all put back
    assert cam.template_vars()["led_ctrl"] == 6 and cam.template_vars()["light_type"] == 1


def test_external_device_is_not_used_when_supplement_light_exists_or_it_has_no_switch():
    cam, fake = _cam()
    fake.docs["/ISAPI/Image/channels/1/supplementLight/capabilities"] = (
        b'<SupplementLight><supplementLightMode opt="colorVuWhiteLight,close">close</supplementLightMode></SupplementLight>')
    fake.docs["/ISAPI/System/externalDevice/capabilities"] = EXTERNAL_CAPS
    asyncio.run(cam.hikcfg.load())
    assert cam.hikcfg.light_on_mode == "colorVuWhiteLight"

    cam, fake = _cam()     # .24 answers an empty capabilities document
    del fake.docs["/ISAPI/Image/channels/1/supplementLight/capabilities"]
    fake.docs["/ISAPI/System/externalDevice/capabilities"] = b"<ExternalDevice></ExternalDevice>"
    asyncio.run(cam.hikcfg.load())
    assert cam.hikcfg.light_on_mode is None


def test_keyframe_is_asked_for_on_a_preview_and_on_msg_189():
    cam, fake = _cam()
    asked = []

    async def request_keyframe(idx):
        asked.append(idx)

    cam.hikcfg.request_keyframe = request_keyframe

    async def run():
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        ok = (await c.request(189, payload=bc.body_xml(
            '<RequestIframe version="1.1">\n<channelId>0</channelId>\n<streamType>subStream</streamType>\n</RequestIframe>'))).response
        await asyncio.sleep(0.05)
        c.writer.close()
        srv.close()
        return ok

    assert asyncio.run(run()) == 200 and asked == [2]


def test_keyframe_request_failing_changes_nothing():
    cam, _ = _cam()

    async def fails(idx):
        raise ConnectionError("HTTP 403")

    cam.hikcfg.request_keyframe = fails
    asyncio.run(cam.request_keyframe("mainStream"))


def test_listen_on_all_interfaces_reports_the_local_ip():
    cam, _ = _cam()
    cam.cfg.listen_host, cam.cfg.hik_host = "0.0.0.0", "127.0.0.1"
    assert cam.ip == "127.0.0.1" and cam.template_vars()["ip"] == cam.ip


FACE_CAPTURE = (b'{\n\t"ipAddress":\t"192.0.2.22",\n\t"channelID":\t1,\n\t"dateTime":\t"2026-11-05T14:19:17-08:00",\n'
                b'\t"eventState":\t"active",\n\t"eventType":\t"faceCapture",\n\t"channelName":\t"Road",\n'
                b'\t"faceCapture":\t[{"faces":\t[{"faceId":\t30,"faceScore":\t40}]}]\n}\n')


def test_face_capture_from_the_alert_stream_is_a_person_and_a_face():
    """As a real camera sends it, a JSON part then JPEG parts: DS-2CD7526G0-IZHS, V5.6.12."""
    from bcproxy import hik
    from bcproxy.events import alarm_event_xml
    jpeg = b"\xff\xd8" + bytes(900_000) + b"\xff\xd9"
    stream = (b'--boundary\r\nContent-Disposition: form-data; name="faceCapture"\r\nContent-Type: application/json\r\n'
              + b"Content-Length: %d\r\n\r\n" % len(FACE_CAPTURE) + FACE_CAPTURE
              + b"--boundary\r\nContent-Disposition: form-data\r\nContent-Type: image/jpeg\r\n"
              + b"Content-Length: %d\r\nContent-ID: faceImage\r\n\r\n" % len(jpeg) + jpeg + b"\r\n--boundary\r\n")
    splitter = hik.MultipartSplitter("boundary")
    parts = []
    for i in range(0, len(stream), 65536):
        parts += splitter.feed(stream[i:i + 65536])
    alerts = [a for a in (hik.parse_part(h, b) for h, b in parts) if a]
    assert [(a.event_type, a.state, a.channel) for a in alerts] == [("faceCapture", "active", 1)]

    cam, _ = _cam(ai_types=["people", "vehicle", "face"])
    cam.on_alert(alerts[0])
    snap = cam.state.snapshot()
    assert snap.motion and snap.ai == {"face", "people"}
    assert "<AItype>people,face</AItype>" in alarm_event_xml(snap)
    assert {k for _t, k in cam.journal.items[-2:]} == {"face", "people"}


def test_face_capture_reports_only_what_the_camera_entry_declares():
    from bcproxy import hik
    cam, _ = _cam(ai_types=["people", "vehicle"])
    cam.on_alert(hik.HikAlert("faceCapture", "active", 1))
    assert cam.state.snapshot().ai == {"people"}


def test_alert_parts_that_are_not_alerts_are_ignored():
    from bcproxy import hik
    assert hik.parse_part({"content-type": "image/jpeg"}, b"\xff\xd8") is None
    assert hik.parse_part({"content-type": "application/json"}, b"not json") is None
    assert hik.parse_part({"content-type": "application/json"}, b'{"a": 1}') is None


def test_199_does_not_declare_an_event_type_version():
    """With it, the playback filter listed Answered Call and Crying Sound (seen on a real NVR)."""
    cam, _ = _cam(ai_types=["people", "vehicle"])
    assert not cam.aitype_bits & 1 << 13        # no tamper
    assert "eventTypeVersion" not in cam.templates.render(199, cam.template_vars()).decode()


def test_end_of_an_undeclared_smart_event_leaves_motion_and_people_alone():
    """Seen on a real camera: linedetection inactive cleared the state while VMD was still active."""
    from bcproxy import hik
    cam, _ = _cam()
    cam.on_alert(hik.HikAlert("VMD", "active", 1))
    cam.on_alert(hik.HikAlert("linedetection", "active", 1, regions=[(1, "human")]))
    cam.on_alert(hik.HikAlert("linedetection", "inactive", 1))
    snap = cam.state.snapshot()
    assert snap.motion and snap.ai == {"people"} and not snap.smart

    cam.on_alert(hik.HikAlert("VMD", "inactive", 1))     # the end of motion itself still clears at once
    snap = cam.state.snapshot()
    assert not snap.motion and not snap.ai


def test_alarms_reach_a_client_that_never_subscribed():
    """A real Reolink NVR and the app were seen logging in without ever sending 31."""
    from bcproxy import hik
    cam, _ = _cam()

    async def run():
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        cam.on_alert(hik.HikAlert("shelteralarm", "active", 1))
        msg = await asyncio.wait_for(c.events.get(), 3)
        c.writer.close()
        srv.close()
        return msg

    msg = asyncio.run(run())
    assert msg.response == 200 and b"<status>tamper</status>" in msg.payload


def test_scene_change_stays_out_of_tamper_unless_configured():
    from bcproxy import hik
    cam, _ = _cam()
    cam.on_alert(hik.HikAlert("scenechangedetection", "active", 1))
    assert not cam.state.snapshot().tamper and not cam.hikcfg.scene_is_tamper


def test_scene_change_as_tamper_on_a_camera_without_people_or_vehicle():
    """DS-2CD2347G1-L: no AI targets; crossline and intrusion come without one, and two detections share Tamper Alert."""
    from bcproxy import hik
    from bcproxy.events import alarm_event_xml
    from bcproxy.templates import smart_detect_xml
    zones = {"crossline": [{"zone": 0, "name": "Line", "targets": []}], "intrusion": [{"zone": 0, "name": "Area", "targets": []}]}
    cam, fake = _cam(ai_types=[], smart_zones=zones, event_map={"sceneChangeDetection": "tamper"})
    tamper, scene = "/ISAPI/System/Video/inputs/channels/1/tamperDetection", "/ISAPI/Smart/SceneChangeDetection/1"
    fake.docs[tamper] = b"<TamperDetection><enabled>false</enabled></TamperDetection>"
    fake.docs[scene] = b"<SceneChangeDetection><id>1</id><enabled>true</enabled></SceneChangeDetection>"

    async def run():
        await cam.hikcfg.load()
        shown = await cam.hikcfg.tamper_enabled()
        await cam.hikcfg.set_tamper(False)
        off = await cam.hikcfg.tamper_enabled()
        await cam.hikcfg.set_tamper(True)
        return shown, off, await cam.hikcfg.tamper_enabled()

    assert asyncio.run(run()) == (True, False, True)    # only scene change on still shows as on
    assert b"<enabled>true" in fake.docs[tamper] and b"<enabled>true" in fake.docs[scene]
    assert cam.hikcfg.has_tamper and cam.aitype_bits == 1 << 25 | 1 << 13 | 1     # no people or vehicle, motion stays

    cam.on_alert(hik.HikAlert("sceneChangeDetection", "active", 1))
    xml = alarm_event_xml(cam.state.snapshot(), report_ai=False, report_smart=True)
    assert "<status>tamper</status>" in xml and "AItype" not in xml

    cam.state = type(cam.state)(holds=cam.cfg.holds)
    cam.on_alert(hik.HikAlert("linedetection", "active", 1, regions=[(1, None)]))
    cam.on_alert(hik.HikAlert("fielddetection", "active", 1, regions=[(1, None)]))
    snap = cam.state.snapshot()
    assert snap.smart == (("crossline", 0, None), ("intrusion", 0, None)) and not snap.ai and snap.motion
    assert xml_value(smart_detect_xml("crossline", zones["crossline"]), "aiType") == "none"
    assert xml_value(smart_detect_xml("crossline", [{"zone": 0}]), "aiType") == "people,vehicle"


def test_camera_without_target_detection_never_reports_a_person():
    """DS-2CD2347G1-L declares people and vehicle only because the NVR's notification page breaks
    without them. Its line crossing and intrusion events have no target and must stay that way."""
    from bcproxy.events import alarm_event_xml
    zones = {k: [{"zone": 0, "name": k, "targets": ["people", "vehicle"]}] for k in ("crossline", "intrusion")}
    cam, _ = _cam(ai_types=["people", "vehicle"], smart_zones=zones)
    for hik_type in ("linedetection", "fielddetection"):
        cam.on_alert(hik.HikAlert(hik_type, "active", 1))
    snap = cam.state.snapshot()
    assert snap.ai == frozenset() and snap.motion
    assert {(k, t) for k, _, t in snap.smart} == {("crossline", None), ("intrusion", None)}
    xml = alarm_event_xml(snap, report_ai=True, report_smart=True)
    assert "<AItype>none</AItype>" in xml and "<type>crossline</type>" in xml and "<type>intrusion</type>" in xml
    assert "people" not in xml and "vehicle" not in xml
