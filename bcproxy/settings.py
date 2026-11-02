"""Persistent camera settings written by the NVR / app ("set" messages).

A real camera stores what the client writes and returns it on the next "get".
We do the same: the XML body of a set message is merged into the current
reply of the matching get message and saved to

    <state_dir>/<camera>/<get id>.xml

which then takes precedence over templates. The camera's display name is kept
in <state_dir>/<camera>/name.txt; stored documents reference it as ${name}, so
a rename via OSD (45) shows up in 80/104/login replies too, and vice versa.
"""
from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from .bc import XML_DECL

log = logging.getLogger("settings")

# set msg id -> get msg id (Baichuan pairs; see reolink_aio / neolink)
SET_TO_GET = {
    25: 26,     # VideoInput / InputAdvanceCfg (image)
    45: 44,     # OSD: OsdChannelName (camera name) / OsdDatetime
    57: 56,     # Compression / encoder
    105: 104,   # SystemGeneral (deviceName, time zone ...)
    209: 208,   # LedState (IR / status LED)
    297: 296,   # DayNight threshold
}

# where a camera name lives in a document
NAME_PATHS = ("OsdChannelName/name", "SystemGeneral/deviceName", "DevInfo/name", "Norm/name")


def get_for_set(set_id: int, payload: bytes, get_doc) -> int | None:
    """get_doc(id) -> current reply bytes or None."""
    if set_id in SET_TO_GET:
        return SET_TO_GET[set_id]
    # common pattern: get N, set N+1 -- only if the written element exists in get N
    doc = get_doc(set_id - 1)
    if not doc:
        return None
    try:
        written = {c.tag for c in _body(payload)}
        current = {c.tag for c in _body(doc)}
    except ET.ParseError:
        return None
    return set_id - 1 if written and written <= current else None


def _body(doc: bytes) -> ET.Element:
    root = ET.fromstring(doc)
    if root.tag != "body":
        b = ET.Element("body")
        b.append(root)
        root = b
    return root


def merge(base: bytes | None, update: bytes) -> bytes:
    """Overwrite leaf values of `base` with those in `update` (same element paths)."""
    upd = _body(update)
    if not base:
        return XML_DECL.encode() + ET.tostring(upd, encoding="unicode").encode() + b"\n"
    root = _body(base)

    def walk(dst: ET.Element, src: ET.Element) -> None:
        for child in src:
            tgt = dst.find(child.tag)
            if tgt is None:
                dst.append(child)
            elif len(child):
                walk(tgt, child)
            else:
                tgt.text = child.text
                tgt.attrib.update(child.attrib)

    walk(root, upd)
    return XML_DECL.encode() + ET.tostring(root, encoding="unicode").encode() + b"\n"


def find_name(doc: bytes) -> str | None:
    try:
        root = _body(doc)
    except ET.ParseError:
        return None
    for p in NAME_PATHS:
        el = root.find(p)
        if el is not None and el.text is not None:
            return el.text
    return None


def templatize_name(doc: bytes, name: str) -> bytes:
    """Replace the literal camera name in name elements by ${name}."""
    text = doc.decode("utf-8")
    esc = name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    for tag in ("name", "deviceName"):
        text = re.sub(rf"<{tag}>{re.escape(esc)}</{tag}>", f"<{tag}>${{name}}</{tag}>", text)
    return text.encode("utf-8")


class SettingsStore:
    def __init__(self, state_dir: str | None, camera: str) -> None:
        self.dir = Path(state_dir) / camera if state_dir else None
        self._mem: dict[int, bytes] = {}
        self._name: str | None = None
        if self.dir and (self.dir / "name.txt").is_file():
            self._name = (self.dir / "name.txt").read_text(encoding="utf-8").strip() or None

    @property
    def name(self) -> str | None:
        return self._name

    def set_name(self, name: str) -> None:
        self._name = name
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "name.txt").write_text(name, encoding="utf-8")

    def get(self, msg_id: int) -> bytes | None:
        if msg_id in self._mem:
            return self._mem[msg_id]
        if self.dir:
            p = self.dir / f"{msg_id}.xml"
            if p.is_file():
                self._mem[msg_id] = p.read_bytes()
                return self._mem[msg_id]
        return None

    def put(self, msg_id: int, doc: bytes) -> None:
        self._mem[msg_id] = doc
        if self.dir:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / f"{msg_id}.xml").write_bytes(doc)
