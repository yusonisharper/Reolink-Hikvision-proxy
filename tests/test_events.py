import xml.etree.ElementTree as ET

from bcproxy import hik
from bcproxy.events import DetEvent, EventState, Holds, alarm_event_xml

ACUSENSE_LINE = b"""<?xml version="1.0" encoding="UTF-8"?>
<EventNotificationAlert version="2.0" xmlns="http://www.hikvision.com/ver20/XMLSchema">
<ipAddress>192.168.1.64</ipAddress><channelID>1</channelID>
<dateTime>2026-10-31T10:00:00+08:00</dateTime><activePostCount>1</activePostCount>
<eventType>linedetection</eventType><eventState>active</eventState>
<DetectionRegionList><DetectionRegionEntry><regionID>2</regionID>
<sensitivityLevel>50</sensitivityLevel><detectionTarget>vehicle</detectionTarget>
</DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>"""

VMD_HUMAN = b"""<EventNotificationAlert xmlns="http://www.hikvision.com/ver20/XMLSchema">
<channelID>1</channelID><eventType>VMD</eventType><eventState>active</eventState>
<DetectionRegionList><DetectionRegionEntry><regionID>1</regionID>
<detectionTarget>human</detectionTarget></DetectionRegionEntry></DetectionRegionList>
</EventNotificationAlert>"""


def test_parse_and_map_line_crossing():
    a = hik.parse_alert(ACUSENSE_LINE)
    assert a.event_type == "linedetection" and a.channel == 1 and a.regions == [(2, "vehicle")]
    evs = hik.EventMapper().map(a)
    assert evs == [DetEvent("crossline", True, 1, frozenset({"vehicle"}))]


def test_map_vmd_human_and_channel_filter():
    a = hik.parse_alert(VMD_HUMAN)
    assert hik.EventMapper(channel=1).map(a)[0].targets == {"people"}
    assert hik.EventMapper(channel=2).map(a) == []


def test_untargeted_smart_event():
    xml = ACUSENSE_LINE.replace(b"<detectionTarget>vehicle</detectionTarget>", b"")
    a = hik.parse_alert(xml)
    assert hik.EventMapper().map(a)[0].targets == frozenset()
    assert hik.EventMapper(untargeted_smart_target="people").map(a)[0].targets == {"people"}


def test_multipart_splitter_chunked_input():
    part = (b"--boundary\r\nContent-Type: application/xml; charset=\"UTF-8\"\r\n"
            b"Content-Length: %d\r\n\r\n" % len(VMD_HUMAN)) + VMD_HUMAN + b"\r\n"
    data = part * 3
    sp = hik.MultipartSplitter("boundary")
    got = []
    for i in range(0, len(data), 37):
        got += sp.feed(data[i:i + 37])
    assert len(got) == 3 and all(b == VMD_HUMAN for _, b in got)


def test_state_hold_and_expiry():
    st = EventState(holds=Holds(motion=3, ai=5, smart=5))
    st.apply(DetEvent("crossline", True, 1, frozenset({"vehicle"})), now=0)
    s = st.snapshot(now=1)
    assert s.motion and s.ai == {"vehicle"} and s.smart == (("crossline", 1, "vehicle"),)
    assert st.snapshot(now=6).idle


def test_vmd_inactive_clears():
    st = EventState()
    st.apply(DetEvent("motion", True, None, frozenset({"people"})), now=0)
    st.apply(DetEvent("motion", False), now=1)
    assert st.snapshot(now=1).idle


def _parse_like_reolink_aio(xml: str):
    """Mirrors reolink_aio baichuan.py cmd_id 33 parsing."""
    root = ET.fromstring("<body>" + xml + "</body>")
    ev = root.find(".//AlarmEvent")
    status = ev.findtext("status")
    ai = ev.findtext("AItype")
    smart = {}
    for sa in ev.findall(".//smartAiType"):
        t = sa.findtext("type")
        mask = int(sa.findtext("index"))
        subs = [(int(s.findtext("index")), s.findtext("type")) for s in sa.findall("subList")]
        smart[t] = (mask, subs)
    return "MD" in status, ai, smart


def test_alarm_xml_is_parseable_like_reolink():
    st = EventState()
    st.apply(DetEvent("crossline", True, 1, frozenset({"vehicle"})), now=0)
    st.apply(DetEvent("intrusion", True, 0, frozenset({"people"})), now=0)
    md, ai, smart = _parse_like_reolink_aio(alarm_event_xml(st.snapshot(now=0)))
    assert md and ai == "people,vehicle"
    assert smart == {"crossline": (0b10, [(1, "vehicle")]), "intrusion": (0b1, [(0, "people")])}
    md, ai, smart = _parse_like_reolink_aio(alarm_event_xml(st.snapshot(now=100)))
    assert not md and ai == "none" and smart == {}
