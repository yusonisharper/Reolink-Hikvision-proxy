"""Normalized detection events -> Reolink msg 33 <AlarmEventList>.

Hikvision delivers edge-triggered or periodically repeated alerts. Reolink
cameras push the *full current state* every time it changes, e.g.

    <AlarmEvent version="1.1">
      <channelId>0</channelId>
      <status>MD</status>                 MD / none (may include visitor, tamper)
      <AItype>people,vehicle</AItype>     people/vehicle/dog_cat/other/none
      <recording>0</recording>
      <timeStamp>0</timeStamp>
      <smartAiTypeList>                   line crossing / intrusion / loitering
        <smartAiType>
          <type>crossline</type>
          <index>1</index>                bitmask of active zones (bit n = zone n)
          <subList><index>0</index><type>people</type></subList>
        </smartAiType>
      </smartAiTypeList>
    </AlarmEvent>

Reference for the field semantics: reolink_aio baichuan.py (cmd_id 33).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

SMART_TYPES = ("crossline", "intrusion", "loitering")
AI_TYPES = ("people", "vehicle", "dog_cat", "face")


@dataclass(frozen=True)
class DetEvent:
    """One normalized detection coming from the source camera."""

    kind: str                      # motion | crossline | intrusion | loitering | tamper
    active: bool = True
    zone: int | None = None        # 0-based Reolink location index
    targets: frozenset[str] = frozenset()   # subset of AI_TYPES


@dataclass(frozen=True)
class Snapshot:
    motion: bool = False
    tamper: bool = False
    ai: frozenset[str] = frozenset()
    smart: tuple[tuple[str, int, str | None], ...] = ()   # (type, zone, target)

    @property
    def idle(self) -> bool:
        return not (self.motion or self.tamper or self.ai or self.smart)


@dataclass
class Holds:
    motion: float = 3.0
    ai: float = 5.0
    smart: float = 5.0


@dataclass
class EventState:
    holds: Holds = field(default_factory=Holds)
    ai_implies_motion: bool = True
    _until: dict[tuple, float] = field(default_factory=dict)

    def apply(self, ev: DetEvent, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        keys: list[tuple[tuple, float]] = []
        if ev.kind == "motion":
            keys.append((("motion",), self.holds.motion))
        elif ev.kind == "tamper":
            keys.append((("tamper",), self.holds.motion))
        elif ev.kind in SMART_TYPES:
            zone = ev.zone or 0
            if ev.targets:
                keys += [(("smart", ev.kind, zone, t), self.holds.smart) for t in ev.targets]
            else:
                keys.append((("smart", ev.kind, zone, None), self.holds.smart))
        for t in ev.targets:
            keys.append((("ai", t), self.holds.ai))

        for key, hold in keys:
            if ev.active:
                self._until[key] = now + hold
            else:
                self._until.pop(key, None)
        if not ev.active and ev.kind == "motion":
            # VMD inactive clears AI derived from motion too
            for k in [k for k in self._until if k[0] == "ai"]:
                self._until.pop(k)

    def snapshot(self, now: float | None = None) -> Snapshot:
        now = time.monotonic() if now is None else now
        for k in [k for k, t in self._until.items() if t <= now]:
            del self._until[k]
        keys = self._until.keys()
        ai = frozenset(k[1] for k in keys if k[0] == "ai")
        smart = tuple(sorted(((k[1], k[2], k[3]) for k in keys if k[0] == "smart"),
                             key=lambda x: (x[0], x[1], x[2] or "")))
        motion = ("motion",) in keys or (self.ai_implies_motion and bool(ai or smart))
        return Snapshot(motion=motion, tamper=("tamper",) in keys, ai=ai, smart=smart)


def alarm_event_xml(snap: Snapshot, channel: int = 0, report_ai: bool = True,
                    report_smart: bool = True) -> str:
    status = []
    if snap.motion:
        status.append("MD")
    if snap.tamper:
        status.append("tamper")
    lines = [
        '<AlarmEventList version="1.1">',
        '<AlarmEvent version="1.1">',
        f"<channelId>{channel}</channelId>",
        f"<status>{','.join(status) or 'none'}</status>",
    ]
    if report_ai:
        ai = ",".join(t for t in AI_TYPES if t in snap.ai)
        lines.append(f"<AItype>{ai or 'none'}</AItype>")
    lines += ["<recording>0</recording>", "<timeStamp>0</timeStamp>"]
    if report_smart and snap.smart:
        lines.append("<smartAiTypeList>")
        for stype in SMART_TYPES:
            items = [s for s in snap.smart if s[0] == stype]
            if not items:
                continue
            mask = 0
            for _, zone, _t in items:
                mask |= 1 << zone
            lines += ["<smartAiType>", f"<type>{stype}</type>", f"<index>{mask}</index>"]
            for _, zone, target in items:
                lines.append("<subList>")
                lines.append(f"<index>{zone}</index>")
                if target:
                    lines.append(f"<type>{target}</type>")
                lines.append("</subList>")
            lines.append("</smartAiType>")
        lines.append("</smartAiTypeList>")
    lines += ["</AlarmEvent>", "</AlarmEventList>"]
    return "\n".join(lines)
