"""Crossline / intrusion rules between the NVR (527-530) and Hikvision Line/FieldDetection."""
import asyncio
import importlib.util
from pathlib import Path

import pytest

from bcproxy import bc
from bcproxy import hiksmart as hs
from bcproxy.bc import xml_value
from bcproxy.session import BcSession

from test_hikcfg import _cam

ROOT = Path(__file__).parent.parent
spec = importlib.util.spec_from_file_location("bc_client", ROOT / "tools" / "bc_client.py")
bc_client = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc_client)

NS = 'xmlns="http://www.hikvision.com/ver20/XMLSchema"'

# DS-2CD2347G1-L (Kitchen), as read on 2026-11-14
G1_LINE = b"""<?xml version="1.0" encoding="UTF-8"?>
<LineDetection>
<id>1</id>
<enabled>true</enabled>
<normalizedScreenSize>
<normalizedScreenWidth>1000</normalizedScreenWidth>
<normalizedScreenHeight>1000</normalizedScreenHeight>
</normalizedScreenSize>
<LineItemList size="1" >
<LineItem>
<id>1</id>
<enabled>false</enabled>
<sensitivityLevel>50</sensitivityLevel>
<directionSensitivity>any</directionSensitivity>
<CoordinatesList>
<Coordinates>
<positionX>0</positionX>
<positionY>0</positionY>
</Coordinates>
<Coordinates>
<positionX>0</positionX>
<positionY>0</positionY>
</Coordinates>
</CoordinatesList>
</LineItem>
</LineItemList>
<isSupportMultiScene>false</isSupportMultiScene>
<recogRuleType>vectorMode</recogRuleType>
</LineDetection>
"""
G1_LINE_CAPS = G1_LINE.replace(b"<enabled>false</enabled>", b'<enabled opt="false,true">false</enabled>').replace(
    b"<directionSensitivity>", b'<directionSensitivity opt="any,left-right,right-left">')
G1_FIELD = f"""<?xml version="1.0" encoding="UTF-8"?>
<FieldDetection version="2.0" {NS}>
<id>1</id>
<enabled>true</enabled>
<FieldDetectionRegionList size="1" >
<FieldDetectionRegion version="2.0" {NS}>
<id>1</id>
<enabled>false</enabled>
<sensitivityLevel>50</sensitivityLevel>
<timeThreshold>0</timeThreshold>
<RegionCoordinatesList>
</RegionCoordinatesList>
</FieldDetectionRegion>
</FieldDetectionRegionList>
</FieldDetection>
""".encode()
G1_FIELD_CAPS = f"""<FieldDetection version="2.0" {NS}><id>1</id><FieldDetectionRegionList size="1" >
<FieldDetectionRegion version="2.0" {NS}><id>1</id><minRegionCoordinatesNum>4</minRegionCoordinatesNum>
<maxRegionCoordinatesNum>4</maxRegionCoordinatesNum><sensitivityLevel min="1" max="100">50</sensitivityLevel>
<timeThreshold min="0" max="10">0</timeThreshold><CoordinatesList></CoordinatesList></FieldDetectionRegion>
</FieldDetectionRegionList></FieldDetection>""".encode()


# DS-2CD2087G2-L (BackDoor): 4 slots, detectionTarget; regions 2-4 have no coordinates list at all
def _g2_field():
    regions = "".join(
        f'<FieldDetectionRegion version="2.0" {NS}><id>{i}</id><enabled>false</enabled>'
        "<sensitivityLevel>50</sensitivityLevel><timeThreshold>0</timeThreshold>"
        + ("<RegionCoordinatesList><RegionCoordinates><positionX>787</positionX><positionY>617</positionY>"
           "</RegionCoordinates></RegionCoordinatesList>" if i == 1 else "")
        + "<detectionTarget>human</detectionTarget></FieldDetectionRegion>" for i in range(1, 5))
    return (f'<FieldDetection version="2.0" {NS}><id>1</id><enabled>false</enabled>'
            f'<FieldDetectionRegionList size="4" >{regions}</FieldDetectionRegionList></FieldDetection>').encode()


def _g2_field_caps():
    regions = "".join(
        f'<FieldDetectionRegion version="2.0" {NS}><id>{i}</id><minRegionCoordinatesNum>4</minRegionCoordinatesNum>'
        "<maxRegionCoordinatesNum>4</maxRegionCoordinatesNum>"
        '<timeThreshold min="0" max="10">0</timeThreshold>'
        '<detectionTarget opt="all,human,vehicle">human</detectionTarget></FieldDetectionRegion>' for i in range(1, 5))
    return (f'<FieldDetection version="2.0" {NS}><id>1</id><FieldDetectionRegionList size="4" >{regions}'
            "</FieldDetectionRegionList><isSupportTargetMultiSelect>true</isSupportTargetMultiSelect>"
            "</FieldDetection>").encode()


LINE = "/ISAPI/Smart/LineDetection/1"
FIELD = "/ISAPI/Smart/FieldDetection/1"


def _crossline(op, points, direction="antiClockwise", ai="people", index=0, name="Fence 2"):
    pts = "".join(f"<point>\n<x>{x}</x>\n<y>{y}</y>\n</point>\n" for x, y in points)
    return bc.body_xml(
        f'<CrosslineDetect version="1.1">\n<channelId>0</channelId>\n<op>{op}</op>\n<separateCfg>\n'
        f"<crossline>0</crossline>\n</separateCfg>\n<crosslineDetectItem>\n<location>{index}</location>\n"
        f"<index>{index}</index>\n<name>{name}</name>\n<aiType>{ai}</aiType>\n<sesensitivity>70</sesensitivity>\n"
        f"<pointList>\n{pts}</pointList>\n<direction>{direction}</direction>\n</crosslineDetectItem>\n"
        "</CrosslineDetect>")


def _intrusion(op, points, ai="people,vehicle,dog_cat", index=0, stay=2):
    pts = "".join(f"<point>\n<x>{x}</x>\n<y>{y}</y>\n</point>\n" for x, y in points)
    return bc.body_xml(
        f'<IntrusionDetect version="1.1">\n<channelId>0</channelId>\n<op>{op}</op>\n<intrusionDetectItem>\n'
        f"<location>{index}</location>\n<index>{index}</index>\n<name>Area 2</name>\n<aiType>{ai}</aiType>\n"
        f"<sesensitivity>70</sesensitivity>\n<stayTime>{stay}</stayTime>\n<pointList>\n{pts}</pointList>\n"
        "</intrusionDetectItem>\n</IntrusionDetect>")


AREA = [(0.08372093, 0.40289256), (0.86976743, 0.13016529), (0.88139534, 0.84504133), (0.20465116, 0.82851237)]


def _session(cam, script):
    async def run():
        await cam.hikcfg.load()
        srv = await asyncio.start_server(lambda r, w: BcSession(cam, r, w).run(), "127.0.0.1", 0)
        c = bc_client.Client("127.0.0.1", srv.sockets[0].getsockname()[1], "admin", "pw")
        await c.connect()
        await c.login()
        try:
            return await script(c)
        finally:
            c.writer.close()
            srv.close()
    return asyncio.run(run())


def _g1_cam(tmp_path=None):
    cam, fake = _cam(ai_types=["people", "vehicle"],
                     smart_zones={"crossline": [{"zone": 0, "name": "Crossline", "targets": ["people", "vehicle"]}]})
    fake.docs.update({LINE: G1_LINE, LINE + "/capabilities": G1_LINE_CAPS,
                      FIELD: G1_FIELD, FIELD + "/capabilities": G1_FIELD_CAPS})
    return cam, fake


def test_coordinates_flip_between_top_left_and_bottom_left():
    assert hs.to_hik(0.25, 0.25) == (250, 750)
    assert hs.from_hik(250, 750) == (0.25, 0.25)
    assert hs.to_hik(-0.1, 1.2) == (0, 0)


def test_line_written_read_back_and_removed():
    cam, fake = _g1_cam()

    async def script(c):
        out = {"empty": (await c.request(527)).payload}
        out["add"] = (await c.request(528, payload=_crossline("add", [(0.2, 0.8), (0.8, 0.2)], "clockwise"))).response
        out["doc"] = fake.docs[LINE]
        out["read"] = (await c.request(527)).payload
        out["delete"] = (await c.request(528, payload=_crossline("delete", []))).response
        out["after"] = (await c.request(527)).payload
        return out

    out = _session(cam, script)
    assert b"<crosslineDetectItem>" not in out["empty"]
    assert out["add"] == 200
    doc = out["doc"].decode()
    assert "<LineItem>\n<id>1</id>\n<enabled>true</enabled>" in doc
    assert "<sensitivityLevel>70</sensitivityLevel>" in doc
    assert f"<directionSensitivity>{hs.DIRECTION_TO_HIK['clockwise']}</directionSensitivity>" in doc
    assert ("<CoordinatesList><Coordinates><positionX>200</positionX><positionY>200</positionY></Coordinates>"
            "<Coordinates><positionX>800</positionX><positionY>800</positionY></Coordinates></CoordinatesList>") in doc
    assert "<recogRuleType>vectorMode</recogRuleType>" in doc       # the rest of the document is kept
    read = out["read"]
    assert xml_value(read, "name") == "Fence 2" and xml_value(read, "aiType") == "people"   # kept by the proxy
    assert xml_value(read, "direction") == "clockwise" and xml_value(read, "sesensitivity") == "70"
    assert b"<x>0.200</x>\n<y>0.800</y>" in read and b"<x>0.800</x>\n<y>0.200</y>" in read
    assert out["delete"] == 200
    assert b"<crosslineDetectItem>" not in out["after"]
    assert "<LineItem>\n<id>1</id>\n<enabled>false</enabled>" in fake.docs[LINE].decode()


@pytest.mark.parametrize("payload", [
    _crossline("add", [(0.82, 0.76), (0.94, 0.50), (0.83, 0.20)], "both"),     # polyline
    _crossline("add", [(0.2, 0.8), (0.8, 0.2)], index=1),                      # G1 has one line
    _crossline("rename", [(0.2, 0.8), (0.8, 0.2)]),
])
def test_line_the_camera_cannot_take_is_refused(payload):
    cam, fake = _g1_cam()

    async def script(c):
        return (await c.request(528, payload=payload)).response

    assert _session(cam, script) == 400
    assert fake.docs[LINE] == G1_LINE


def test_existing_camera_line_gets_the_configured_name():
    cam, fake = _g1_cam()
    fake.docs[LINE] = G1_LINE.replace(b"<enabled>false</enabled>", b"<enabled>true</enabled>").replace(
        b"<positionX>0</positionX>\n<positionY>0</positionY>", b"<positionX>907</positionX>\n<positionY>512</positionY>", 1)

    async def script(c):
        return (await c.request(527)).payload

    read = _session(cam, script)
    assert xml_value(read, "name") == "Crossline" and xml_value(read, "aiType") == "people,vehicle"
    assert xml_value(read, "direction") == "both"
    assert b"<x>0.907</x>\n<y>0.488</y>" in read


def test_area_on_g1_and_point_count():
    cam, fake = _g1_cam()

    async def script(c):
        ok = (await c.request(530, payload=_intrusion("add", AREA, stay=20))).response
        triangle = (await c.request(530, payload=_intrusion("add", AREA[:3]))).response
        return ok, triangle, (await c.request(529)).payload

    ok, triangle, read = _session(cam, script)
    assert ok == 200 and triangle == 400
    doc = fake.docs[FIELD].decode()
    assert "<timeThreshold>10</timeThreshold>" in doc                 # Hikvision's maximum
    assert doc.count("<RegionCoordinates>") == 4 and "<positionX>84</positionX><positionY>597</positionY>" in doc
    assert xml_value(read, "stayTime") == "10" and xml_value(read, "aiType") == "people,vehicle,dog_cat"


def test_g2_targets_go_to_the_camera_and_missing_coordinates_list_is_added():
    cam, fake = _cam(ai_types=["people", "vehicle"])
    fake.docs.update({FIELD: _g2_field(), FIELD + "/capabilities": _g2_field_caps()})

    async def script(c):
        both = (await c.request(530, payload=_intrusion("add", AREA, ai="people,vehicle", index=2))).response
        doc_both = fake.docs[FIELD].decode()
        car = (await c.request(530, payload=_intrusion("modify", AREA, ai="vehicle", index=2))).response
        return both, doc_both, car, (await c.request(529)).payload

    both, doc_both, car, read = _session(cam, script)
    assert both == 200 and car == 200
    region3 = doc_both.split("<id>3</id>")[1].split("</FieldDetectionRegion>")[0]
    assert "<detectionTarget>all</detectionTarget>" in region3
    assert region3.count("<RegionCoordinates>") == 4
    assert doc_both.startswith('<FieldDetection version="2.0" ' + NS + "><id>1</id><enabled>true</enabled>")
    doc = fake.docs[FIELD].decode()
    assert "<detectionTarget>vehicle</detectionTarget>" in doc.split("<id>3</id>")[1]
    assert xml_value(read, "index") == "2" and xml_value(read, "aiType") == "vehicle"


def test_targets_mapping():
    caps = hs.Caps(slots=4, targets=["human", "vehicle", "others"], multi_target=True)
    assert hs.targets_to_hik(["people", "dog_cat"], caps) == "human,others"
    assert hs.targets_from_hik("others,vehicle,human", caps) == ["dog_cat", "vehicle", "people"]
    single = hs.Caps(slots=1, targets=["all", "human", "vehicle"])
    assert hs.targets_to_hik(["people", "vehicle"], single) == "all"
    assert hs.targets_from_hik("all", single) == ["people", "vehicle"]
    assert hs.targets_to_hik([], single) == "all"


def test_camera_without_line_detection_keeps_the_configured_reply():
    cam, fake = _cam(smart_zones={"crossline": [{"zone": 0, "name": "Crossline", "targets": ["people"]}]})

    async def script(c):
        return (await c.request(527)).payload

    read = _session(cam, script)
    assert xml_value(read, "name") == "Crossline" and b"<pointList>" not in read
    assert "crossline" not in cam.hikcfg.smart_caps
