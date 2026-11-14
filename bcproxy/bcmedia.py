"""BcMedia: the container Reolink cameras put inside msg 3 binary payloads.

Reference: neolink crates/core/src/bcmedia/{model,ser}.rs
"""
from __future__ import annotations

import struct
import time
from dataclasses import dataclass

from . import hevcps

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


def iframe(codec: str, data: bytes, micros: int, unix_time: int | None = None, file_no: int = 0,
           ext: bytes = b"") -> bytes:
    """file_no: last header field. Live, it is the high half of the time (micros >> 32). In replay real
    cameras change it when the next recording file starts (same value for all frames of a file).
    ext: the extension data of the frame (motion boxes), after the time."""
    extra = (b"" if unix_time is None else struct.pack("<II", unix_time & 0xFFFFFFFF, 0)) + ext + _pad(len(ext))
    hdr = struct.pack("<I4sIIII", MAGIC_IFRAME, codec.encode(), len(data), len(extra),
                      micros & 0xFFFFFFFF, file_no)
    return hdr + extra + data + _pad(len(data))


def pframe(codec: str, data: bytes, micros: int, file_no: int = 0, ext: bytes = b"") -> bytes:
    extra = ext + _pad(len(ext))
    hdr = struct.pack("<I4sIIII", MAGIC_PFRAME, codec.encode(), len(data), len(extra), micros & 0xFFFFFFFF, file_no)
    return hdr + extra + data + _pad(len(data))


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
                out.append((kind, {"codec": codec.decode(errors="replace"), "micros": micros, "file_no": file_no,
                                   "extra": bytes(self.buf[24:24 + extra])}, data_))
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
    slices: int = 1      # VCL NAL units (slices) in the picture


NO_START_CODE_LIMIT = 1 << 20    # bytes kept while no start code has come at all


class _NalSplitter:
    """Annex-B bytes -> complete NAL units (with 4-byte start codes). Scans each byte once: a NAL unit
    of a big keyframe arrives over many reads, and searching it again from its start for every read
    cost more than the rest of the parsing."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.nal = -1       # where the payload of the unfinished NAL unit starts in buf (-1: none yet)
        self.scan = 0       # where the search for the next start code goes on

    def feed(self, data: bytes) -> list[bytes]:
        buf = self.buf
        buf += data
        out = []
        i, nal, begin = self.scan, self.nal, 0
        find = buf.find
        while (j := find(b"\x00\x00\x01", i)) >= 0:
            s = j - 1 if j > 0 and buf[j - 1] == 0 else j
            if nal >= 0:
                # rstrip: trailing_zero_8bits (Dahua pads with them) are not part of a NAL unit. A Reolink
                # NVR plays them, but the mp4 it makes for a download then has no I frames.
                out.append(b"\x00\x00\x00\x01" + buf[nal:s].rstrip(b"\x00"))
            begin, nal = s, j + 3
            i = nal
        if nal < 0 and len(buf) > NO_START_CODE_LIMIT:
            begin = len(buf) - 3
        if begin:
            del buf[:begin]          # cheap: CPython moves the start of a bytearray instead of copying
            i -= begin
            if nal >= 0:
                nal -= begin
        self.nal = nal
        self.scan = max(i, len(buf) - 2, 0)   # a start code can begin in the last two bytes
        return out

    @property
    def rest(self) -> bytes:
        return bytes(self.buf)


def _split_nals(buf: bytes) -> tuple[list[bytes], bytes]:
    """Return complete NAL units (with 4-byte start codes) and the remainder."""
    s = _NalSplitter()
    return s.feed(buf), s.rest


class AnnexBParser:
    def __init__(self, codec: str) -> None:
        assert codec in ("H264", "H265")
        self.codec = codec
        self.splitter = _NalSplitter()
        self.cur: list[bytes] = []          # NAL units of the access unit being collected
        self.cur_types: set[int] = set()    # their types
        self.cur_slices = 0                 # their VCL NAL units
        self.cur_key = False
        self.params: dict[int, bytes] = {}   # nal type -> latest VPS/SPS/PPS
        self.flat_ok: dict[bytes, bool] = {}       # H.265 SPS -> may declare one sub-layer (hevcps)
        self.flat_cache: dict[tuple[bytes, bool], bytes] = {}   # H.265: (original VPS/SPS, flatten) -> sent

    # -- NAL classification ---------------------------------------------------
    def _info(self, nal: bytes) -> tuple[int, bool, bool, bool]:
        """(type, is_vcl, is_key, first_slice_of_picture). Indexes nal: a slice copied to read
        its header would copy the whole picture."""
        n = len(nal)
        if n <= 4:
            return -1, False, False, False
        if self.codec == "H264":
            t = nal[4] & 0x1F
            vcl = 1 <= t <= 5
            first = vcl and n > 5 and bool(nal[5] & 0x80)   # first_mb_in_slice == 0
            return t, vcl, t == 5, first
        t = (nal[4] >> 1) & 0x3F
        vcl = t < 32
        first = vcl and n > 6 and bool(nal[6] & 0x80)       # first_slice_segment_in_pic_flag
        return t, vcl, 16 <= t <= 23, first

    def _is_param(self, t: int) -> bool:
        return t in ((7, 8) if self.codec == "H264" else (32, 33, 34))

    def _is_aud(self, t: int) -> bool:
        return t == (9 if self.codec == "H264" else 35)

    def _flatten_sub_layers(self, nals: list[bytes]) -> list[bytes]:
        """H.265 VPS/SPS of an access unit, rewritten for the NVR (see hevcps):
        - one temporal sub-layer, when the stream's SPS allows it (hevcps.flattenable); VPS and SPS are
          rewritten together or not at all;
        - an SPS longer than Reolink's mp4 writer accepts loses its VUI HRD parameters."""
        sps = next((n for n in nals if self._info(n)[0] == 33), None) or self.params.get(33)
        if sps is None:
            return nals
        ok = self.flat_ok.get(sps)
        if ok is None:
            if len(self.flat_ok) > 8:
                self.flat_ok.clear()
                self.flat_cache.clear()
            ok = self.flat_ok[sps] = hevcps.flattenable(sps)
        out = []
        for n in nals:
            t = self._info(n)[0]
            if t in (32, 33):
                new = self.flat_cache.get((n, ok))
                if new is None:
                    new = hevcps.single_sub_layer(n) if ok else n
                    if t == 33:
                        new = hevcps.fit_mp4_limit(new)
                    self.flat_cache[(n, ok)] = new
                n = new
            out.append(n)
        return out

    # -- feeding --------------------------------------------------------------
    def feed(self, data: bytes) -> list[AccessUnit]:
        out: list[AccessUnit] = []
        for nal in self.splitter.feed(data):
            t, vcl, key, first = self._info(nal)
            param = self._is_param(t)
            if self.cur_slices and (first or param or self._is_aud(t)):
                out.append(self._flush())
            if param:
                self.params[t] = nal
            self.cur.append(nal)
            self.cur_types.add(t)
            if vcl:
                self.cur_slices += 1
                self.cur_key |= key
        return out

    def _flush(self) -> AccessUnit:
        nals = self.cur
        key = self.cur_key
        if key:
            missing = [self.params[t] for t in sorted(self.params) if t not in self.cur_types]
            nals = missing + nals
        if self.codec == "H265":
            nals = self._flatten_sub_layers(nals)
        au = AccessUnit(b"".join(nals), key, self.cur_slices)
        self.cur, self.cur_types, self.cur_slices, self.cur_key = [], set(), 0, False
        return au
