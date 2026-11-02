"""Protocol tests against real-camera samples taken from neolink's test suite."""
from pathlib import Path

from bcproxy import bc, bcmedia
from bcproxy.bc import BcMsg, xml_value
from bcproxy.crypto import (
    Crypter,
    Enc,
    aes_decrypt,
    aes_encrypt,
    bc_xor,
    login_hash,
    make_aes_key,
)

S = Path(__file__).parent / "samples"


def test_bc_xor_matches_real_sample():
    enc = (S / "xml_crypto_sample1.bin").read_bytes()
    plain = (S / "xml_crypto_sample1_plaintext.bin").read_bytes()
    assert bc_xor(0, enc) == plain
    assert bc_xor(0, plain) == enc


def test_decode_real_nonce_reply():
    d = bc.Decoder(Crypter())
    d.feed((S / "model_sample_modern_login.bin").read_bytes())
    m = d.next()
    assert (m.msg_id, m.response, m.cls, len(m.raw_body)) == (1, 0xDD01, 0x6614, 145)
    assert xml_value(m.payload, "nonce") == "9E6D1FCB9E69846D"


def test_decode_real_legacy_login():
    d = bc.Decoder(Crypter())
    d.feed((S / "model_sample_legacy_login.bin").read_bytes())
    m = d.next()
    assert m.msg_id == 1 and m.cls == bc.CLASS_LEGACY and len(m.payload) == 1836


def test_decode_real_video_start_binary_mode():
    cr = Crypter()
    cr.set(Enc.BC)
    d = bc.Decoder(cr)
    d.feed((S / "modern_video_start1.bin").read_bytes())
    d.feed((S / "modern_video_start2.bin").read_bytes())
    m1, m2 = d.next(), d.next()
    assert m1.binary and len(m1.payload) == 32 and xml_value(m1.ext, "binaryData") == "1"
    assert m2.binary and len(m2.payload) == 30344 and not m2.ext
    # first payload is a BcMedia InfoV1/V2 header
    items = bcmedia.BcMediaReader().feed(m1.payload)
    assert items and items[0][0] == "info"


def test_decode_b800_preview_with_channel_offset_138():
    cr = Crypter()
    cr.set(Enc.BC)
    d = bc.Decoder(cr)
    d.feed((S / "xml_mainstream_b800.bin").read_bytes())
    m = d.next()
    assert (m.msg_id, m.channel, m.cls) == (3, 138, 0x6414)
    assert xml_value(m.payload, "streamType") == "mainStream"
    d.feed((S / "xml_substream_b800.bin").read_bytes())
    assert xml_value(d.next().payload, "streamType") == "subStream"


def test_encode_decode_roundtrip_all_modes():
    for mode in (Enc.NONE, Enc.BC, Enc.AES):
        a, b = Crypter(), Crypter()
        key = make_aes_key("ABCDEF", "pw") if mode == Enc.AES else None
        a.set(mode, key)
        b.set(mode, key)
        msg = BcMsg(199, 7, 0, 42, 200, bc.CLASS_MODERN, bc.ext_xml(channelId=0),
                    bc.body_xml("<Support><x>1</x></Support>"))
        d = bc.Decoder(b)
        d.feed(bc.encode(msg, a))
        out = d.next()
        assert (out.msg_id, out.channel, out.msg_num, out.response) == (199, 7, 42, 200)
        assert out.ext == msg.ext and out.payload == msg.payload


def test_aes_matches_reference_construction():
    # AES-128-CFB128, IV 0123456789abcdef, key = MD5("nonce-password") hex upper [:16]
    key = make_aes_key("13BCECE33DA453DB", "secret")
    assert len(key) == 16 and key == key.upper()
    data = b"<?xml version=\"1.0\"?><body/>"
    assert aes_decrypt(key, aes_encrypt(key, data)) == data
    # per-section restart: encrypting twice gives identical ciphertext
    assert aes_encrypt(key, data) == aes_encrypt(key, data)


def test_login_hash_is_31_upper_hex():
    h = login_hash("admin", "9E6D1FCB9E69846D")
    assert len(h) == 31 and h == h.upper()


def test_bcmedia_roundtrip_with_real_frames():
    iframe_raw = b"".join((S / f"argus2_iframe_{i}.raw").read_bytes() for i in range(5))
    items = bcmedia.BcMediaReader().feed(iframe_raw)
    assert items and items[0][0] == "I"
    kind, meta, data = items[0]
    rebuilt = bcmedia.iframe(meta["codec"], data, meta["micros"])
    items2 = bcmedia.BcMediaReader().feed(rebuilt)
    assert items2[0][2] == data
    pf = sorted(S.glob("argus2_pframe_*.raw"), key=lambda f: int(f.stem.rsplit("_", 1)[1]))
    p = bcmedia.BcMediaReader().feed(b"".join(f.read_bytes() for f in pf))
    assert [k for k, _, _ in p] == ["P", "audio"]   # real stream interleaves AAC


def test_annexb_access_units_h264():
    sps, pps = b"\x00\x00\x00\x01\x67\x42", b"\x00\x00\x00\x01\x68\xce"
    idr = b"\x00\x00\x00\x01\x65\x88\x84"
    p1 = b"\x00\x00\x00\x01\x41\x9a\x01"
    p2 = b"\x00\x00\x00\x01\x41\x9a\x02"
    parser = bcmedia.AnnexBParser("H264")
    aus = parser.feed(sps + pps + idr + p1 + p2 + sps + idr)
    assert [a.keyframe for a in aus] == [True, False, False]
    assert aus[0].data.startswith(sps + pps)
