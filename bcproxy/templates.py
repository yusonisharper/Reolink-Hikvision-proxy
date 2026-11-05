"""Reply templates for "get" style Baichuan messages.

Lookup order for msg <id>:
  1. <templates_dir>/<camera name>/<id>.xml   (per camera override)
  2. <templates_dir>/<id>.xml                 (e.g. captured from a real camera with tools/bc_dump.py)
  3. bcproxy/templates/<id>.xml               (built-in defaults)

A file may be a full document (starting with "<?xml") or just the inner
elements, which are wrapped into <body>. "${var}" placeholders are filled from
the camera context (see VirtualCamera.template_vars).
"""
from __future__ import annotations

import logging
from pathlib import Path
from string import Template

from .bc import XML_DECL, body_xml

log = logging.getLogger("templates")
BUILTIN = Path(__file__).parent / "templates"


class Templates:
    def __init__(self, camera: str, user_dir: str | None = None) -> None:
        self.dirs: list[Path] = []
        if user_dir:
            self.dirs += [Path(user_dir) / camera, Path(user_dir)]
        self.dirs.append(BUILTIN)
        self._cache: dict[str, str | None] = {}

    def _load(self, key: str) -> str | None:
        if key not in self._cache:
            self._cache[key] = None
            for d in self.dirs:
                p = d / f"{key}.xml"
                if p.is_file():
                    self._cache[key] = p.read_text(encoding="utf-8")
                    log.debug("template %s <- %s", key, p)
                    break
        return self._cache[key]

    def has(self, key: str | int) -> bool:
        return self._load(str(key)) is not None

    def render(self, key: str | int, vars: dict) -> bytes | None:
        text = self._load(str(key))
        if text is None:
            return None
        text = Template(text).safe_substitute({k: str(v) for k, v in vars.items()})
        if text.lstrip().startswith("<?xml"):
            return text.encode()
        return body_xml(text)


SMART_MSG = {527: "crossline", 529: "intrusion", 531: "loitering"}
SMART_ROOT = {"crossline": "CrosslineDetect", "intrusion": "IntrusionDetect", "loitering": "LoiteringDetect"}


def smart_detect_xml(stype: str, zones: list[dict], channel: int = 0) -> bytes:
    """Default reply for 527/529/531 (zone list for line crossing/intrusion/loitering).

    The exact schema of real cameras includes geometry (line/polygon points);
    capture one from a real camera for best NVR compatibility. reolink_aio only
    relies on location/name/sesensitivity/aiType/index/stayTime/timeThresh.
    """
    root = SMART_ROOT[stype]
    items = []
    for i, z in enumerate(zones):
        loc = int(z.get("zone", i))
        targets = ",".join(z["targets"]) or "none" if "targets" in z else "people,vehicle"
        extra = ""
        if stype == "loitering":
            extra = f"<timeThresh>{int(z.get('delay', 10))}</timeThresh>\n"
        elif stype == "intrusion":
            extra = f"<stayTime>{int(z.get('delay', 0))}</stayTime>\n"
        items.append(
            f"<{stype}DetectItem>\n"
            f"<location>{loc}</location>\n"
            f"<index>{loc}</index>\n"
            f"<enable>1</enable>\n"
            f"<name>{z.get('name', f'{stype}{loc}')}</name>\n"
            f"<sesensitivity>{int(z.get('sensitivity', 50))}</sesensitivity>\n"
            f"<aiType>{targets}</aiType>\n"
            f"{extra}"
            f"</{stype}DetectItem>")
    inner = (f'<{root} version="1.1">\n<channelId>{channel}</channelId>\n<op>modify</op>\n'
             + "\n".join(items) + f"\n</{root}>")
    return body_xml(inner)


__all__ = ["Templates", "smart_detect_xml", "SMART_MSG", "XML_DECL"]
