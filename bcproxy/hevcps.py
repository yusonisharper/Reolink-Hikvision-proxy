"""Declare a single temporal sub-layer in H.265 VPS/SPS.

Hikvision DS-2CD2347G1-L declares 2 temporal sub-layers in its VPS/SPS (even with SVC off), sends its
P frames with TemporalId 1 and 2, and keeps a long-term reference picture. In playback and download
transcoding, the Reolink NVR's Novatek decoder is opened with max_ref_num 1. It has an SVC reference
handling for streams that declare more than one sub-layer, and the G1 stream then fails with
"H265 - not found a reference picture or hdal_max_ref_num (1) error"; nothing is decoded until the next
IDR.

Hikvision DS-2CD7526G0-IZHS sends the same kind of stream (P frames with TemporalId 1 and 2, a long-term
reference) but declares one sub-layer, and the NVR plays it. Declaring one sub-layer makes the G1 stream
look like that. Decoders that decode every picture, as the NVR and ffmpeg do, produce the same pictures.
Only the VPS/SPS bytes change; slices are passed through untouched. Streams with real temporal layers
are left alone (see flattenable).

Syntax: ITU-T H.265 7.3.2.1 (VPS), 7.3.2.2 (SPS), 7.3.3 (profile_tier_level), 7.3.4 (scaling list),
7.3.7 (st_ref_pic_set), E.2.1 (VUI), E.2.2/E.2.3 (HRD).
"""
from __future__ import annotations

VPS, SPS = 32, 33


class _Bits:
    """Reads an RBSP and writes the transformed RBSP at the same time."""

    def __init__(self, rbsp: bytes) -> None:
        self.data = rbsp
        self.pos = 0
        self.out: list[int] = []

    # -- reading ------------------------------------------------------------------------------
    def u(self, n: int) -> int:
        v = 0
        for _ in range(n):
            byte = self.data[self.pos >> 3]          # IndexError past the end -> caller gives up
            v = (v << 1) | ((byte >> (7 - (self.pos & 7))) & 1)
            self.pos += 1
        return v

    def ue(self) -> int:
        zeros = 0
        while self.u(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("bad exp-golomb")
        return (1 << zeros) - 1 + self.u(zeros)

    def se(self) -> int:
        k = self.ue()
        return (k + 1) // 2 if k & 1 else -(k // 2)

    # -- writing ------------------------------------------------------------------------------
    def put(self, n: int, v: int) -> None:
        for i in range(n - 1, -1, -1):
            self.out.append((v >> i) & 1)

    def put_ue(self, v: int) -> None:
        v += 1
        n = v.bit_length()
        self.put(n - 1, 0)
        self.put(n, v)

    def put_se(self, v: int) -> None:
        self.put_ue(2 * v - 1 if v > 0 else -2 * v)

    # -- copying ------------------------------------------------------------------------------
    def cu(self, n: int) -> int:
        v = self.u(n)
        self.put(n, v)
        return v

    def cue(self) -> int:
        v = self.ue()
        self.put_ue(v)
        return v

    def cse(self) -> int:
        v = self.se()
        self.put_se(v)
        return v

    def copy_tail(self) -> bytes:
        """Copy the remaining syntax up to rbsp_stop_one_bit, then terminate and byte-align."""
        last = len(self.data) * 8 - 1
        while last >= self.pos and not (self.data[last >> 3] >> (7 - (last & 7))) & 1:
            last -= 1
        if last < self.pos:
            raise ValueError("no rbsp_stop_one_bit")
        while self.pos < last:
            self.cu(1)
        self.put(1, 1)
        while len(self.out) % 8:
            self.out.append(0)
        return bytes(int("".join(map(str, self.out[i:i + 8])), 2) for i in range(0, len(self.out), 8))


def _unescape(payload: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for b in payload:
        if zeros >= 2 and b == 3:
            zeros = 0
            continue
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


def _escape(rbsp: bytes) -> bytes:
    out = bytearray()
    zeros = 0
    for b in rbsp:
        if zeros >= 2 and b <= 3:
            out.append(3)
            zeros = 0
        out.append(b)
        zeros = zeros + 1 if b == 0 else 0
    return bytes(out)


# -- syntax structures --------------------------------------------------------------------------

def _ptl(b: _Bits, max_sub: int) -> None:
    """profile_tier_level(1, max_sub), written as profile_tier_level(1, 0)."""
    b.cu(32); b.cu(32); b.cu(32)                     # general profile/tier/flags/level: 96 bits
    prof, lvl = [], []
    for _ in range(max_sub):
        prof.append(b.u(1)); lvl.append(b.u(1))
    if max_sub > 0:
        b.u(2 * (8 - max_sub))                        # reserved_zero_2bits
    for i in range(max_sub):
        if prof[i]:
            b.u(32); b.u(32); b.u(24)                 # sub_layer profile: 88 bits
        if lvl[i]:
            b.u(8)


def _ordering(b: _Bits, max_sub: int) -> None:
    """*_sub_layer_ordering_info: keep only the values for the highest sub-layer (largest DPB)."""
    present = b.u(1)
    vals = None
    for _ in range(0 if present else max_sub, max_sub + 1):
        vals = (b.ue(), b.ue(), b.ue())
    b.put(1, present)
    for v in vals:
        b.put_ue(v)


def _sub_layer_hrd(b: _Bits, cpb_cnt: int, sub_pic: int, write: bool) -> None:
    for _ in range(cpb_cnt + 1):
        for _ in range(4 if sub_pic else 2):
            (b.cue if write else b.ue)()
        (b.cu if write else b.u)(1)


def _hrd(b: _Bits, common: int, max_sub: int) -> None:
    """hrd_parameters(common, max_sub), written with sub-layer 0 only."""
    nal = vcl = sub_pic = 0
    if common:
        nal, vcl = b.cu(1), b.cu(1)
        if nal or vcl:
            sub_pic = b.cu(1)
            if sub_pic:
                b.cu(8); b.cu(5); b.cu(1); b.cu(5)
            b.cu(4); b.cu(4)
            if sub_pic:
                b.cu(4)
            b.cu(5); b.cu(5); b.cu(5)
    for i in range(max_sub + 1):
        w = i == 0
        c1 = b.cu if w else b.u
        cue = b.cue if w else b.ue
        general = c1(1)
        within = 1 if general else c1(1)
        low_delay = 0
        if within:
            cue()
        else:
            low_delay = c1(1)
        cpb_cnt = 0 if low_delay else cue()
        if nal:
            _sub_layer_hrd(b, cpb_cnt, sub_pic, w)
        if vcl:
            _sub_layer_hrd(b, cpb_cnt, sub_pic, w)


def _scaling_list(b: _Bits) -> None:
    for size in range(4):
        for _ in range(0, 6, 3 if size == 3 else 1):
            if not b.cu(1):
                b.cue()
            else:
                if size > 1:
                    b.cse()
                for _ in range(min(64, 1 << (4 + (size << 1)))):
                    b.cse()


def _st_rps(b: _Bits, idx: int, num_delta: list[int]) -> None:
    if idx and b.cu(1):                               # inter_ref_pic_set_prediction_flag
        b.cu(1); b.cue()                              # delta_rps_sign, abs_delta_rps_minus1
        n = 0
        for _ in range(num_delta[idx - 1] + 1):
            used = b.cu(1)
            use_delta = 1 if used else b.cu(1)
            n += 1 if (used or use_delta) else 0
        num_delta.append(n)                           # upper bound is enough to keep parsing in step
        return
    neg, pos = b.cue(), b.cue()
    for _ in range(neg + pos):
        b.cue(); b.cu(1)
    num_delta.append(neg + pos)


def _vui(b: _Bits, max_sub: int, drop_hrd: bool = False) -> None:
    if b.cu(1) and b.cu(8) == 255:                    # aspect_ratio_info
        b.cu(16); b.cu(16)
    if b.cu(1):                                       # overscan_info_present
        b.cu(1)
    if b.cu(1):                                       # video_signal_type_present
        b.cu(3); b.cu(1)
        if b.cu(1):
            b.cu(8); b.cu(8); b.cu(8)
    if b.cu(1):                                       # chroma_loc_info_present
        b.cue(); b.cue()
    b.cu(1); b.cu(1); b.cu(1)                         # neutral_chroma, field_seq, frame_field_info
    if b.cu(1):                                       # default_display_window
        b.cue(); b.cue(); b.cue(); b.cue()
    if b.cu(1):                                       # vui_timing_info_present
        b.cu(32); b.cu(32)
        if b.cu(1):
            b.cue()
        if not drop_hrd:
            if b.cu(1):
                _hrd(b, 1, max_sub)
        elif b.u(1):                                  # vui_hrd_parameters_present_flag: read, write 0
            b.put(1, 0)
            mark = len(b.out)
            _hrd(b, 1, max_sub)                       # parse to skip it, then drop what it copied
            del b.out[mark:]
        else:
            b.put(1, 0)
    # bitstream_restriction and the rest do not depend on the sub-layer count: copy_tail


def _rewrite_vps(b: _Bits) -> None:
    b.cu(4); b.cu(1); b.cu(1); b.cu(6)                # id, base_layer flags, max_layers_minus1
    max_sub = b.u(3); b.u(1)
    b.put(3, 0); b.put(1, 1)                          # one sub-layer; temporal_id_nesting must be 1
    b.cu(16)
    _ptl(b, max_sub)
    _ordering(b, max_sub)
    max_layer_id = b.cu(6)
    for _ in range(b.cue()):                          # vps_num_layer_sets_minus1
        b.cu(max_layer_id + 1)
    if b.cu(1):                                       # vps_timing_info_present
        b.cu(32); b.cu(32)
        if b.cu(1):
            b.cue()
        for i in range(b.cue()):                      # vps_num_hrd_parameters
            b.cue()
            common = b.cu(1) if i > 0 else 1
            _hrd(b, common, max_sub)


def _rewrite_sps(b: _Bits, drop_hrd: bool = False) -> list[int]:
    """Rewrite the SPS; return NumDeltaPocs of its short-term reference picture sets."""
    b.cu(4)
    max_sub = b.u(3); b.u(1)
    b.put(3, 0); b.put(1, 1)
    _ptl(b, max_sub)
    b.cue()                                           # sps_seq_parameter_set_id
    if b.cue() == 3:                                  # chroma_format_idc
        b.cu(1)
    b.cue(); b.cue()                                  # width, height
    if b.cu(1):
        b.cue(); b.cue(); b.cue(); b.cue()
    b.cue(); b.cue()                                  # bit depths
    poc_lsb_bits = b.cue() + 4
    _ordering(b, max_sub)
    for _ in range(6):                                # coding/transform block sizes, hierarchy depths
        b.cue()
    if b.cu(1) and b.cu(1):                           # scaling_list_enabled, sps_scaling_list_data_present
        _scaling_list(b)
    b.cu(1); b.cu(1)                                  # amp, sao
    if b.cu(1):                                       # pcm_enabled
        b.cu(4); b.cu(4); b.cue(); b.cue(); b.cu(1)
    num_delta: list[int] = []
    for i in range(b.cue()):                          # num_short_term_ref_pic_sets
        _st_rps(b, i, num_delta)
    if b.cu(1):                                       # long_term_ref_pics_present
        for _ in range(b.cue()):
            b.cu(poc_lsb_bits); b.cu(1)
    b.cu(1); b.cu(1)                                  # temporal_mvp, strong_intra_smoothing
    if b.cu(1):                                       # vui_parameters_present
        _vui(b, max_sub, drop_hrd)
    return num_delta


def flattenable(sps: bytes) -> bool:
    """True when the SPS declares several sub-layers but no reference picture set keeps more than one
    short-term picture: the shape of DS-2CD2347G1-L and DS-2CD7526G0-IZHS.

    Real temporal scalability (Hikvision SVC on, e.g. DS-2CD2347G2-LU) keeps two short-term pictures in
    some sets. The NVR plays those live only in its SVC mode, so their parameter sets must stay as they
    are."""
    h = sps[4:]
    if len(h) < 4 or (h[0] >> 1) & 0x3F != SPS:
        return False
    try:
        if max_sub_layers_minus1(sps) == 0:
            return False
        b = _Bits(_unescape(h[2:]))
        return max(_rewrite_sps(b), default=0) <= 1
    except (IndexError, ValueError):
        return False


def max_sub_layers_minus1(nal: bytes) -> int:
    """sps/vps_max_sub_layers_minus1 of a VPS or SPS NAL unit (start code included)."""
    h = nal[4:]
    t = (h[0] >> 1) & 0x3F
    rbsp = _unescape(h[2:6])
    return (rbsp[1] >> 1) & 7 if t == VPS else (rbsp[0] >> 1) & 7


MP4_PS_LIMIT = 64   # bytes, start code included


def fit_mp4_limit(nal: bytes) -> bytes:
    """Return the SPS NAL unit (4-byte start code included) without its VUI HRD parameters when it is
    longer than MP4_PS_LIMIT.

    Reolink's mp4 writer (bc_mp4_process_nalu, in the NVR's recorder and in the phone app's BCSDK)
    gives up on a VPS/SPS/PPS longer than 64 bytes, start code included: the NVR then writes 0-byte
    files and the app reports a download error. DS-2CD2347G1-L sends a 64-byte SPS (68 with the start
    code), 81 with H.265+; about 18 of them are HRD parameters. Decoders do not use HRD parameters, and
    the camera sends no buffering-period or picture-timing SEI that would refer to them.

    Only single-sub-layer SPS are changed (after single_sub_layer when that applies); the input is
    returned unchanged when it is short enough, is not such an SPS, or cannot be parsed."""
    h = nal[4:]
    if len(nal) <= MP4_PS_LIMIT or len(h) < 4 or (h[0] >> 1) & 0x3F != SPS:
        return nal
    try:
        if max_sub_layers_minus1(nal) != 0:
            return nal
        b = _Bits(_unescape(h[2:]))
        _rewrite_sps(b, drop_hrd=True)
        rbsp = b.copy_tail()
    except (IndexError, ValueError):
        return nal
    return nal[:6] + _escape(rbsp)


def single_sub_layer(nal: bytes) -> bytes:
    """Return the VPS/SPS NAL unit (4-byte start code included) rewritten to one temporal sub-layer.

    Returns the input unchanged when it already declares one sub-layer, is not a VPS/SPS, or cannot be
    parsed."""
    h = nal[4:]
    if len(h) < 4:
        return nal
    t = (h[0] >> 1) & 0x3F
    if t not in (VPS, SPS):
        return nal
    try:
        if max_sub_layers_minus1(nal) == 0:
            return nal
        b = _Bits(_unescape(h[2:]))
        (_rewrite_vps if t == VPS else _rewrite_sps)(b)
        rbsp = b.copy_tail()
    except (IndexError, ValueError):
        return nal
    return nal[:6] + _escape(rbsp)
