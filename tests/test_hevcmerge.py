"""H.265 tile merging (bcproxy/hevcmerge.py, bcproxy/native/hevcmerge.c).

The library tests need bcproxy/native/libhevcmerge-<cpu>.so (bcproxy/native/build.sh); without it they
are skipped. The sample is a Hikvision DS-2CD2347G1-L main stream (2688x1520, 3 tiles of one slice each):
VPS/SPS/PPS, an IDR picture and a P picture.

Checked outside these tests (2026-11-13), on captures of Backyard, Camera23, MainDoor (rotated,
1520x2688) and MountainView (DS-2CD2347G2-LU): every picture merged; ffmpeg decodes the merged streams
to the same frames (framemd5); the bin sequence of an IDR slice matches libde265's; Reolink's mp4 writer
(libBCSDKWrapper.dll) writes GOPs of 250 pictures with all frames; 6000 corrupted access units under
AddressSanitizer/UBSan neither crash nor leak.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bcproxy import hevcmerge
from bcproxy.bcmedia import AccessUnit, AnnexBParser

SAMPLE = os.path.join(os.path.dirname(__file__), "samples", "hik_tiles.h265")
SC = b"\x00\x00\x00\x01"
AUD = SC + b"\x46\x01\x50"
# an AUD ends the picture before it; the parser sees a NAL unit once the next start code arrives
FLUSH = AUD + AUD
MERGED = SC + b"\x26\x01merged"


def access_units(data: bytes) -> list[AccessUnit]:
    return AnnexBParser("H265").feed(data + FLUSH)


def sample_aus() -> list[AccessUnit]:
    with open(SAMPLE, "rb") as f:
        return access_units(f.read())


# -- stream integration (no library needed) ----------------------------------------------------------

class FakeMerger:
    def __init__(self, result):
        self.result = result
        self.seen = []

    def merge(self, au):
        self.seen.append(au)
        return self.result


def run_merger(result, aus):
    async def go():
        m = hevcmerge.StreamMerger("test")
        m.merger = FakeMerger(result)
        try:
            return [await m(au) for au in aus], m.merger.seen
        finally:
            m.close()
    return asyncio.run(go())


def test_merged_picture_leaves_as_one_slice():
    aus = sample_aus()
    assert [au.slices for au in aus] == [3, 3]
    out, seen = run_merger(MERGED, aus)
    assert [(au.slices, au.keyframe, au.data) for au in out] == [(1, True, MERGED), (1, False, MERGED)]
    assert seen == [au.data for au in aus]


def test_picture_stays_as_it_came_when_merge_declines():
    aus = sample_aus()
    out, _ = run_merger(None, aus)
    assert out == aus


def test_single_slice_pictures_are_not_touched():
    au = AccessUnit(SC + b"\x26\x01\xaf\x00", True, 1)
    out, seen = run_merger(b"never", [au])
    assert out == [au] and seen == []


def test_config_switch_turns_merging_off(monkeypatch):
    monkeypatch.setattr(hevcmerge, "enabled", False)
    assert not hevcmerge.available()
    assert hevcmerge.stream_merger("x", "H265") is None


def test_no_merging_for_h264():
    assert hevcmerge.stream_merger("x", "H264") is None


def test_library_path_names_the_cpu():
    assert hevcmerge.library_path("AMD64").endswith("libhevcmerge-x86_64.so")
    assert hevcmerge.library_path("aarch64").endswith("libhevcmerge-aarch64.so")


# -- the library ------------------------------------------------------------------------------------

needs_lib = pytest.mark.skipif(not hevcmerge.available(), reason="libhevcmerge not built for this machine")


def vcl(au: bytes) -> list[bytes]:
    return [n for n in au.split(SC) if n and (n[0] >> 1) & 0x3F < 32]


def params(au: bytes) -> list[bytes]:
    return [n for n in au.split(SC) if n and (n[0] >> 1) & 0x3F >= 32]


@needs_lib
def test_merges_idr_and_p_picture_into_one_slice():
    aus = [au.data for au in sample_aus()]
    assert len(aus) == 2
    m = hevcmerge.TileMerger("test", verify=True)
    for au in aus:
        out = m.merge(au)
        assert out is not None
        assert len(vcl(out)) == 1
        assert params(out) == params(au)          # parameter sets are kept as they came
        assert abs(len(out) - len(au)) < 64       # new header and entry points, two start codes fewer
    assert m.merged == 2 and not m.failed


@needs_lib
def test_merged_slice_is_the_first_slice_of_the_picture():
    au = sample_aus()[0].data
    out = hevcmerge.TileMerger(verify=True).merge(au)
    merged = vcl(out)[0]
    assert merged[:2] == vcl(au)[0][:2]           # same NAL unit header
    assert merged[2] & 0x80                       # first_slice_segment_in_pic_flag
    assert len(merged) > sum(len(s) for s in vcl(au)) - 3 * 8


@needs_lib
def test_p_picture_before_parameter_sets_is_left_alone():
    p_only = sample_aus()[1].data
    m = hevcmerge.TileMerger()
    assert m.merge(p_only) is None
    assert m.failed == {hevcmerge.NO_PARAMS: 1}


@needs_lib
def test_corrupt_slice_is_left_alone():
    idr = bytearray(sample_aus()[0].data)
    idr[len(idr) // 2] ^= 0xFF
    m = hevcmerge.TileMerger(verify=True)
    assert m.merge(bytes(idr)) is None
    assert m.failed and m.merged == 0
