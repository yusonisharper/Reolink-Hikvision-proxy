"""BcMedia: the container Reolink cameras put inside msg 3 binary payloads.

Reference: neolink crates/core/src/bcmedia/{model,ser}.rs
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

MAGIC_INFO_V1 = 0x31303031
MAGIC_INFO_V2 = 0x32303031
MAGIC_IFRAME = 0x63643030
MAGIC_PFRAME = 0x63643130
MAGIC_AAC = 0x62773530
PAD = 8


def _pad(n: int) -> bytes:
    r = n % PAD
    return b"" if r == 0 else bytes(PAD - r)


def info_v2(width: int, height: int, fps: int) -> bytes:
    t = time.localtime()
    ymdhms = bytes([(t.tm_year - 1900) & 0xFF, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec])
    return (struct.pack("<IIIIBB", MAGIC_INFO_V2, 32, width, height, 0, fps & 0xFF)
            + ymdhms + ymdhms + b"\x00\x00")


def iframe(codec: str, data: bytes, micros: int, unix_time: int | None = None, file_no: int = 0) -> bytes:
    """file_no: last header field. In replay real cameras change it when the next recording file
    starts (same value for all frames of a file); that is how the app notices the switch."""
    extra = b"" if unix_time is None else struct.pack("<II", unix_time & 0xFFFFFFFF, 0)
    hdr = struct.pack("<I4sIIII", MAGIC_IFRAME, codec.encode(), len(data), len(extra),
                      micros & 0xFFFFFFFF, file_no)
    return hdr + extra + data + _pad(len(data))


def pframe(codec: str, data: bytes, micros: int, file_no: int = 0) -> bytes:
    hdr = struct.pack("<I4sIIII", MAGIC_PFRAME, codec.encode(), len(data), 0, micros & 0xFFFFFFFF, file_no)
    return hdr + data + _pad(len(data))


def aac(adts: bytes) -> bytes:
    return struct.pack("<IHH", MAGIC_AAC, len(adts), len(adts)) + adts + _pad(len(adts))


class BcMediaReader:
    """Parse a BcMedia byte stream (used by the test client / pcap tools)."""

    def __init__(self) -> None:
        self.buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[str, dict, bytes]]:
        self.buf += data
        out = []
        while len(self.buf) >= 8:
            magic = struct.unpack_from("<I", self.buf)[0]
            if magic in (MAGIC_INFO_V1, MAGIC_INFO_V2):
                if len(self.buf) < 32:
                    break
                _, _, w, h, _, fps = struct.unpack_from("<IIIIBB", self.buf)
                out.append(("info", {"width": w, "height": h, "fps": fps}, b""))
                del self.buf[:32]
            elif (magic & 0xFFFF0000) == 0x63640000 and ((magic >> 8) & 0xFF) in (0x30, 0x31):
                if len(self.buf) < 24:
                    break
                _, codec, ln, extra, micros, file_no = struct.unpack_from("<I4sIIII", self.buf)
                total = 24 + extra + ln + (-ln % PAD)
                if len(self.buf) < total:
                    break
                data_ = bytes(self.buf[24 + extra:24 + extra + ln])
                kind = "I" if ((magic >> 8) & 0xFF) == 0x30 else "P"
                out.append((kind, {"codec": codec.decode(errors="replace"), "micros": micros, "file_no": file_no},
                            data_))
                del self.buf[:total]
            elif magic in (MAGIC_AAC, 0x62773130):
                ln = struct.unpack_from("<H", self.buf, 4)[0]
                total = 8 + ln + (-ln % PAD)
                if len(self.buf) < total:
                    break
                out.append(("audio", {}, bytes(self.buf[8:8 + ln])))
                del self.buf[:total]
            else:
                # resync: skip a byte
                del self.buf[:1]
        return out


# ---------------------------------------------------------------------------
# Annex-B access unit splitter (H.264 / H.265)
# ---------------------------------------------------------------------------

@dataclass
class AccessUnit:
    data: bytes          # Annex-B, start codes included
    keyframe: bool


def _split_nals(buf: bytes) -> tuple[list[bytes], bytes]:
    """Return complete NAL units (with 4-byte start codes) and the remainder."""
    starts = []
    i, n = 0, len(buf)
    while i + 3 <= n:
        j = buf.find(b"\x00\x00\x01", i)
        if j < 0:
            break
        s = j - 1 if j > 0 and buf[j - 1] == 0 else j
        starts.append((s, j + 3))
        i = j + 3
    if len(starts) < 2:
        return [], buf
    nals = [b"\x00\x00\x00\x01" + buf[starts[k][1]:starts[k + 1][0]] for k in range(len(starts) - 1)]
    return nals, buf[starts[-1][0]:]


class AnnexBParser:
    def __init__(self, codec: str) -> None:
        assert codec in ("H264", "H265")
        self.codec = codec
        self.rem = b""
        self.cur: list[bytes] = []
        self.cur_has_vcl = False
        self.cur_key = False
        self.params: dict[int, bytes] = {}   # nal type -> latest VPS/SPS/PPS

    # -- NAL classification ---------------------------------------------------
    def _info(self, nal: bytes) -> tuple[int, bool, bool, bool]:
        """(type, is_vcl, is_key, first_slice_of_picture)"""
        h = nal[4:]
        if not h:
            return -1, False, False, False
        if self.codec == "H264":
            t = h[0] & 0x1F
            vcl = 1 <= t <= 5
            first = vcl and len(h) > 1 and bool(h[1] & 0x80)   # first_mb_in_slice == 0
            return t, vcl, t == 5, first
        t = (h[0] >> 1) & 0x3F
        vcl = t < 32
        first = vcl and len(h) > 2 and bool(h[2] & 0x80)       # first_slice_segment_in_pic_flag
        return t, vcl, 16 <= t <= 23, first

    def _is_param(self, t: int) -> bool:
        return t in ((7, 8) if self.codec == "H264" else (32, 33, 34))

    def _is_aud(self, t: int) -> bool:
        return t == (9 if self.codec == "H264" else 35)

    # -- feeding --------------------------------------------------------------
    def feed(self, data: bytes) -> list[AccessUnit]:
        nals, self.rem = _split_nals(self.rem + data)
        out: list[AccessUnit] = []
        for nal in nals:
            t, vcl, key, first = self._info(nal)
            starts_new = self.cur_has_vcl and (self._is_aud(t) or first or (not vcl and t not in (-1,)
                                                                                and self._is_param(t)))
            if starts_new:
                out.append(self._flush())
            if self._is_param(t):
                self.params[t] = nal
            self.cur.append(nal)
            if vcl:
                self.cur_has_vcl = True
                self.cur_key |= key
        return out

    def _flush(self) -> AccessUnit:
        nals = self.cur
        key = self.cur_key
        if key:
            present = {self._info(n)[0] for n in nals}
            missing = [self.params[t] for t in sorted(self.params) if t not in present]
            nals = missing + nals
        au = AccessUnit(b"".join(nals), key)
        self.cur, self.cur_has_vcl, self.cur_key = [], False, False
        return au
