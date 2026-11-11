"""H.265 VPS/SPS rewrite to one temporal sub-layer (bcproxy.hevcps).

Samples are the parameter sets of a DS-2CD2347G1-L (2 sub-layers) and a DS-2CD2347G2-LU with SVC on
(4 sub-layers). g1_params_flat.h265 is the expected output; ffmpeg decoded 6 s of the G1 stream with the
rewritten parameter sets to the same frames (framemd5) as the original.
"""
from pathlib import Path

from bcproxy import hevcps
from bcproxy.bcmedia import AnnexBParser, _split_nals

S = Path(__file__).parent / "samples"
SC = b"\x00\x00\x00\x01"


def _nals(name: str) -> list[bytes]:
    nals, rem = _split_nals((S / name).read_bytes() + SC)
    return nals


def test_g1_parameter_sets_match_expected_output():
    orig, flat = _nals("g1_params.h265"), _nals("g1_params_flat.h265")
    assert [hevcps.max_sub_layers_minus1(n) for n in orig[:2]] == [1, 1]
    assert [hevcps.single_sub_layer(n) for n in orig] == flat
    assert [hevcps.max_sub_layers_minus1(n) for n in flat[:2]] == [0, 0]


def test_four_sub_layer_parameter_sets_parse():
    orig = _nals("mv_params.h265")
    flat = [hevcps.single_sub_layer(n) for n in orig]
    assert [hevcps.max_sub_layers_minus1(n) for n in flat[:2]] == [0, 0]
    assert flat[2] is orig[2]                         # PPS untouched


def test_already_single_layer_and_other_nals_unchanged():
    flat = _nals("g1_params_flat.h265")
    for n in flat:                                    # VPS, SPS (already one sub-layer) and PPS
        assert hevcps.single_sub_layer(n) is n
    idr = SC + b"\x26\x01\xaf\x00"
    assert hevcps.single_sub_layer(idr) is idr


def test_unparsable_parameter_set_passed_through():
    vps = _nals("g1_params.h265")[0]
    cut = vps[:12]
    assert hevcps.single_sub_layer(cut) is cut


def test_escape_roundtrip():
    rbsp = b"\x00\x00\x00\x00\x01\x00\x00\x02\x00\x00\x03\x80"
    assert hevcps._unescape(hevcps._escape(rbsp)) == rbsp
    assert b"\x00\x00\x00" not in hevcps._escape(rbsp)


def test_flattenable_only_without_real_temporal_layers():
    assert hevcps.flattenable(_nals("g1_params.h265")[1])          # one short-term ref per set
    assert not hevcps.flattenable(_nals("mv_params.h265")[1])      # SVC on: a set keeps 2 pictures
    assert not hevcps.flattenable(_nals("g1_params_flat.h265")[1])  # already one sub-layer
    assert not hevcps.flattenable(_nals("g1_params.h265")[0])      # not an SPS


def _feed(params: bytes) -> tuple[AnnexBParser, list]:
    idr = SC + b"\x26\x01\xaf\x00\x11"
    p = AnnexBParser("H265")
    aus = p.feed(params + idr + SC + b"\x02\x05\xd0\x22" + SC + b"\x40\x01")
    return p, aus


def test_parser_rewrites_g1_parameter_sets():
    # one sub-layer, then no HRD in the SPS (g1_params_mp4.h265 = fit_mp4_limit of g1_params_flat.h265)
    p, aus = _feed((S / "g1_params.h265").read_bytes())
    assert aus[0].keyframe
    assert aus[0].data.startswith((S / "g1_params_mp4.h265").read_bytes())


def test_parser_rewrites_parameter_sets_it_prepends():
    p, _ = _feed((S / "g1_params.h265").read_bytes())
    # IDR without in-band SPS/PPS, then a P frame so the IDR's access unit is complete
    aus = p.feed(SC + b"\x26\x01\xaf\x00\x33" + SC + b"\x02\x01\xd0\x44" + SC + b"\x40\x01")
    aus = [a for a in aus if a.keyframe]
    new_sps, orig_sps = _nals("g1_params_mp4.h265")[1], _nals("g1_params.h265")[1]
    assert aus[0].keyframe
    assert new_sps in aus[0].data and orig_sps not in aus[0].data


def test_fit_mp4_limit_drops_hrd_of_long_sps_only():
    flat = _nals("g1_params_flat.h265")
    sps = flat[1]
    assert len(sps) > hevcps.MP4_PS_LIMIT
    short = hevcps.fit_mp4_limit(sps)
    assert len(short) <= hevcps.MP4_PS_LIMIT
    assert short == _nals("g1_params_mp4.h265")[1]
    assert hevcps.fit_mp4_limit(short) is short                     # already short enough
    assert hevcps.fit_mp4_limit(flat[0]) is flat[0]                 # VPS
    assert hevcps.fit_mp4_limit(flat[2]) is flat[2]                 # PPS
    multi = _nals("mv_params.h265")[1]                               # several sub-layers: left alone
    assert hevcps.fit_mp4_limit(multi + bytes(64)) == multi + bytes(64)


def test_parser_keeps_parameter_sets_within_mp4_limit():
    p, aus = _feed((S / "g1_params.h265").read_bytes())
    nals, _ = _split_nals(aus[0].data + SC)
    assert all(len(n) <= hevcps.MP4_PS_LIMIT for n in nals if (n[4] >> 1) & 0x3F in (32, 33, 34))


def test_parser_leaves_real_svc_stream_alone():
    orig = (S / "mv_params.h265").read_bytes()
    p, aus = _feed(orig)
    assert aus[0].data.startswith(orig)
