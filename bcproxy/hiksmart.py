"""Line crossing and intrusion rules: Reolink 527/528 and 529/530 <-> Hikvision LineDetection and
FieldDetection.

Reolink, from NVR captures (2026-11-14):
  points    fractions 0..1, origin top left
  direction "both", or the arrow as start->end turned 90 degrees "clockwise" / "antiClockwise" on screen
  aiType    the targets ticked, e.g. "people,vehicle"
  op        "add" / "modify" / "delete", by <index>
  a line with more than two points is a polyline: Hikvision only has straight lines, so it is refused

Hikvision: points 0..1000, origin bottom left; a fixed number of rule slots (1 on G1, 4 on G2); a region
needs between min/maxRegionCoordinatesNum points; detectionTarget exists on AcuSense models only.
A rule's own <enabled> is not kept (a G1 answers OK to true and still reads false; lines drawn in its web
page read false too): a slot is a rule when the detection is on and the slot has a shape. Removing a rule
clears its points, and switches the detection off when no rule is left.
Names, and the targets of models without detectionTarget, are kept by the proxy.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape as xml_escape

from .bc import body_xml

CROSSLINE, INTRUSION = "crossline", "intrusion"
READ_IDS = {527: CROSSLINE, 529: INTRUSION}
WRITE_IDS = {528: CROSSLINE, 530: INTRUSION}
META_KEYS = {CROSSLINE: 100527, INTRUSION: 100529}    # settings store slots for names and targets
DEFAULT_NAME = {CROSSLINE: "Fence", INTRUSION: "Area"}

# Reolink direction -> Hikvision directionSensitivity
DIRECTION_TO_HIK = {"both": "any", "clockwise": "right-left", "antiClockwise": "left-right"}
DIRECTION_FROM_HIK = {v: k for k, v in DIRECTION_TO_HIK.items()}
TARGET_TO_HIK = {"people": "human", "vehicle": "vehicle", "dog_cat": "others"}
TARGET_FROM_HIK = {v: k for k, v in TARGET_TO_HIK.items()}


@dataclass(frozen=True)
class Kind:
    root: str          # Reolink document
    path: str          # Hikvision document, {ch} = channel
    item: str          # Hikvision rule element
    coords: str        # its coordinates list
    coord: str         # one point in it
    after: str         # element the coordinates list follows when it is missing


KINDS = {
    CROSSLINE: Kind("CrosslineDetect", "/ISAPI/Smart/LineDetection/{ch}", "LineItem",
                    "CoordinatesList", "Coordinates", "directionSensitivity"),
    INTRUSION: Kind("IntrusionDetect", "/ISAPI/Smart/FieldDetection/{ch}", "FieldDetectionRegion",
                    "RegionCoordinatesList", "RegionCoordinates", "timeThreshold"),
}


@dataclass
class Caps:
    slots: int
    min_points: int = 2
    max_points: int = 2
    max_stay: int = 10
    targets: list[str] = field(default_factory=list)    # Hikvision detectionTarget options; empty = none
    multi_target: bool = False


@dataclass
class Rule:
    index: int                                    # 0-based, Hikvision id - 1
    points: list[tuple[float, float]]             # Reolink fractions, origin top left
    sensitivity: int = 50
    direction: str = "both"                       # crossline
    stay: int = 0                                 # intrusion, seconds
    targets: list[str] | None = None              # Reolink names; None: the camera has no target filter
    name: str = ""


class RuleError(ValueError):
    """The NVR asked for something the camera cannot do."""


def _root(doc: bytes | str) -> ET.Element:
    text = doc.decode("utf-8") if isinstance(doc, bytes) else doc
    root = ET.fromstring(re.sub(r'\sxmlns(:\w+)?="[^"]*"', "", text, count=0))
    return root


def parse_caps(kind: str, doc: bytes) -> Caps:
    k, root = KINDS[kind], _root(doc)
    items = root.findall(f".//{k.item}")
    if not items:
        raise ValueError(f"no {k.item} in capabilities")
    first = items[0]
    caps = Caps(slots=len(items))
    if kind == INTRUSION:
        caps.min_points = int(first.findtext("minRegionCoordinatesNum") or 3)
        caps.max_points = int(first.findtext("maxRegionCoordinatesNum") or 10)
        t = first.find("timeThreshold")
        caps.max_stay = int(t.get("max", 10)) if t is not None else 10
    target = first.find("detectionTarget")
    if target is not None and target.get("opt"):
        caps.targets = target.get("opt").split(",")
    caps.multi_target = (root.findtext("isSupportTargetMultiSelect") or "").strip() == "true"
    return caps


def to_hik(x: float, y: float) -> tuple[int, int]:
    clamp = lambda v: max(0, min(1000, round(v * 1000)))  # noqa: E731
    return clamp(x), clamp(1 - y)


def from_hik(x: int, y: int) -> tuple[float, float]:
    return x / 1000, 1 - y / 1000


def targets_from_hik(value: str, caps: Caps) -> list[str]:
    names = [o for o in caps.targets if o != "all"] if value.strip() == "all" else value.split(",")
    return [TARGET_FROM_HIK[n] for n in (x.strip() for x in names) if n in TARGET_FROM_HIK]


def targets_to_hik(targets: list[str], caps: Caps) -> str:
    opts = [o for o in caps.targets if o != "all"]
    wanted = [TARGET_TO_HIK[t] for t in targets if TARGET_TO_HIK.get(t) in opts] or opts
    if "all" in caps.targets and set(wanted) == set(opts) and len(opts) > 1:
        return "all"
    if len(wanted) > 1 and not caps.multi_target:
        return "all" if "all" in caps.targets else wanted[0]
    return ",".join(o for o in opts if o in wanted)


def rules_from_hik(kind: str, doc: bytes, caps: Caps) -> list[Rule]:
    """The slots with a shape, while the detection is on."""
    k, out, root = KINDS[kind], [], _root(doc)
    if (root.findtext("enabled") or "").strip() != "true":
        return out
    for item in root.findall(f".//{k.item}"):
        pts = _points(item, k)
        if not _has_shape(kind, pts):
            continue
        target = item.findtext("detectionTarget")
        out.append(Rule(
            index=int(item.findtext("id") or 1) - 1,
            points=[from_hik(x, y) for x, y in pts],
            sensitivity=int(item.findtext("sensitivityLevel") or 50),
            direction=DIRECTION_FROM_HIK.get((item.findtext("directionSensitivity") or "any").strip(), "both"),
            stay=int(item.findtext("timeThreshold") or 0),
            targets=targets_from_hik(target, caps) if target is not None and caps.targets else None))
    return out


def _points(item: ET.Element, k: Kind) -> list[tuple[int, int]]:
    return [(int(c.findtext("positionX") or 0), int(c.findtext("positionY") or 0))
            for c in item.findall(f"{k.coords}/{k.coord}")]


def _has_shape(kind: str, pts: list[tuple[int, int]]) -> bool:
    return sum(1 for x, y in pts if x or y) >= (2 if kind == CROSSLINE else 3)


def rules_xml(kind: str, rules: list[Rule], channel: int = 0) -> bytes:
    """Reply to 527/529: the item layout of the NVR's own writes, plus <enable>."""
    k, items = KINDS[kind], []
    for r in rules:
        pts = "".join(f"<point>\n<x>{x:.3f}</x>\n<y>{y:.3f}</y>\n</point>\n" for x, y in r.points)
        extra = f"<direction>{r.direction}</direction>\n" if kind == CROSSLINE else f"<stayTime>{r.stay}</stayTime>\n"
        items.append(
            f"<{kind}DetectItem>\n<location>{r.index}</location>\n<index>{r.index}</index>\n<enable>1</enable>\n"
            f"<name>{xml_escape(r.name)}</name>\n<aiType>{','.join(r.targets or []) or 'none'}</aiType>\n"
            f"<sesensitivity>{r.sensitivity}</sesensitivity>\n<pointList>\n{pts}</pointList>\n{extra}"
            f"</{kind}DetectItem>\n")
    return body_xml(f'<{k.root} version="1.1">\n<channelId>{channel}</channelId>\n<op>modify</op>\n'
                    + "".join(items) + f"</{k.root}>")


def parse_write(kind: str, payload: bytes) -> tuple[str, Rule]:
    """NVR 528/530 -> (op, rule)."""
    root = _root(payload)
    doc = root if root.tag == KINDS[kind].root else root.find(KINDS[kind].root)
    if doc is None:
        raise RuleError(f"no <{KINDS[kind].root}>")
    item = doc.find(f"{kind}DetectItem")
    if item is None:
        raise RuleError(f"no <{kind}DetectItem>")
    op = (doc.findtext("op") or "").strip()
    pts = [(float(p.findtext("x") or 0), float(p.findtext("y") or 0)) for p in item.findall("pointList/point")]
    ai = (item.findtext("aiType") or "").strip()
    return op, Rule(
        index=int(item.findtext("index") or item.findtext("location") or 0),
        points=pts,
        sensitivity=int(item.findtext("sesensitivity") or 50),
        direction=(item.findtext("direction") or "both").strip(),
        stay=int(item.findtext("stayTime") or 0),
        targets=[t for t in ai.split(",") if t and t != "none"],
        name=(item.findtext("name") or "").strip())


def check(kind: str, rule: Rule, caps: Caps) -> None:
    if not 0 <= rule.index < caps.slots:
        raise RuleError(f"rule {rule.index + 1}: the camera has {caps.slots} rule(s)")
    n = len(rule.points)
    if kind == CROSSLINE:
        if n != 2:
            raise RuleError(f"a line of {n} points: the camera only takes straight lines (2 points)")
        if rule.direction not in DIRECTION_TO_HIK:
            raise RuleError(f"unknown direction {rule.direction!r}")
    elif not caps.min_points <= n <= caps.max_points:
        raise RuleError(f"an area of {n} points: the camera takes "
                        + (f"{caps.min_points}" if caps.min_points == caps.max_points
                           else f"{caps.min_points} to {caps.max_points}"))


def _set_in(text: str, tag: str, value) -> str:
    new, n = re.subn(rf"(<{tag}(?:\s[^>]*)?>)[^<]*(</{tag}>)", rf"\g<1>{value}\g<2>", text, count=1)
    return new if n else text


def apply(kind: str, doc: bytes, rule: Rule | None, index: int, caps: Caps) -> bytes:
    """The Hikvision document with slot `index` set to `rule`, or cleared for None."""
    k, text = KINDS[kind], doc.decode("utf-8")
    pattern = re.compile(rf"<{k.item}(?=[\s>]).*?</{k.item}>", re.S)
    for m in pattern.finditer(text):
        block = m.group(0)
        idm = re.search(r"<id>(\d+)</id>", block)
        if not idm or int(idm.group(1)) != index + 1:
            continue
        block = _set_in(block, "enabled", "false" if rule is None else "true")
        if rule is None:
            # a line keeps its two points, as an unused G1 slot has them; a region is left empty
            zero = f"<{k.coord}><positionX>0</positionX><positionY>0</positionY></{k.coord}>" * 2
            block = re.sub(rf"<{k.coords}(?:\s[^>]*)?>.*?</{k.coords}>",
                           lambda _: f"<{k.coords}>{zero if kind == CROSSLINE else ''}</{k.coords}>",
                           block, count=1, flags=re.S)
        else:
            block = _set_in(block, "sensitivityLevel", max(1, min(100, rule.sensitivity)))
            if kind == CROSSLINE:
                block = _set_in(block, "directionSensitivity", DIRECTION_TO_HIK[rule.direction])
            else:
                block = _set_in(block, "timeThreshold", max(0, min(caps.max_stay, rule.stay)))
            pts = "".join(f"<{k.coord}><positionX>{x}</positionX><positionY>{y}</positionY></{k.coord}>"
                          for x, y in (to_hik(*p) for p in rule.points))
            coords = f"<{k.coords}>{pts}</{k.coords}>"
            block, n = re.subn(rf"<{k.coords}(?:\s[^>]*)?>.*?</{k.coords}>|<{k.coords}\s*/>",
                               lambda _: coords, block, count=1, flags=re.S)
            if not n:
                block, n = re.subn(rf"(</{k.after}>)", lambda mm: mm.group(1) + coords, block, count=1)
                if not n:
                    raise RuleError(f"no place for {k.coords} in {k.item} {index + 1}")
            if caps.targets and rule.targets is not None and "<detectionTarget" in block:
                block = _set_in(block, "detectionTarget", targets_to_hik(rule.targets, caps))
        text = text[:m.start()] + block + text[m.end():]
        # the first <enabled> switches the detection as a whole: on with a rule, off when none is left
        if rule is not None:
            text = _set_in(text, "enabled", "true")
        elif not any(_has_shape(kind, _points(item, k)) for item in _root(text).findall(f".//{k.item}")):
            text = _set_in(text, "enabled", "false")
        return text.encode("utf-8")
    raise RuleError(f"no {k.item} {index + 1} in the camera's document")


# ------------------------------------------------------------------ names and targets kept by the proxy
def meta_from(doc: bytes | None) -> dict[int, dict]:
    if not doc:
        return {}
    out = {}
    for r in _root(doc).iter("rule"):
        out[int(r.findtext("index") or 0)] = {"name": r.findtext("name") or "",
                                              "targets": [t for t in (r.findtext("aiType") or "").split(",") if t]}
    return out


def meta_doc(meta: dict[int, dict]) -> bytes:
    rules = "".join(f"<rule>\n<index>{i}</index>\n<name>{xml_escape(m['name'])}</name>\n"
                    f"<aiType>{','.join(m['targets'])}</aiType>\n</rule>\n" for i, m in sorted(meta.items()))
    return body_xml(f"<SmartRuleMeta>\n{rules}</SmartRuleMeta>")
