#!/usr/bin/env python3
"""Decrypt a captured Reolink NVR <-> camera session (pcap / pcapng).

Capture (camera password needed for AES):
    tcpdump -i eth0 -w nvr.pcap host <camera-ip> and tcp port 9000
Then:
    bc_dump.py nvr.pcap -p <camera password> --templates captured/

Prints every message (both directions, decrypted) and writes the camera's
200-OK XML replies as captured/<msg_id>.xml, which bcproxy then serves
instead of its built-in defaults. Also prints a summary of every msg id the
NVR used -- that is the list the proxy must answer.
"""
from __future__ import annotations

import argparse
import os
import struct
import sys
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bcproxy import bc  # noqa: E402
from bcproxy.bc import xml_value  # noqa: E402
from bcproxy.crypto import Crypter, Enc, make_aes_key  # noqa: E402


# ----------------------------------------------------------------- pcap read
def read_packets(path: str):
    data = open(path, "rb").read()
    magic = data[:4]
    if magic in (b"\xd4\xc3\xb2\xa1", b"\xa1\xb2\xc3\xd4", b"\x4d\x3c\xb2\xa1", b"\xa1\xb2\x3c\x4d"):
        e = "<" if magic in (b"\xd4\xc3\xb2\xa1", b"\x4d\x3c\xb2\xa1") else ">"
        linktype = struct.unpack(e + "I", data[20:24])[0]
        off = 24
        while off + 16 <= len(data):
            _, _, incl, _ = struct.unpack(e + "IIII", data[off:off + 16])
            yield linktype, data[off + 16:off + 16 + incl]
            off += 16 + incl
    elif magic == b"\x0a\x0d\x0d\x0a":
        off, links, e = 0, [], "<"
        while off + 12 <= len(data):
            btype = struct.unpack_from("<I", data, off)[0]
            if btype == 0x0A0D0D0A:
                e = "<" if data[off + 8:off + 12] == b"\x4d\x3c\x2b\x1a" else ">"
            btype, blen = struct.unpack_from(e + "II", data, off)
            if blen < 12:
                break
            if btype == 1:
                links.append(struct.unpack_from(e + "H", data, off + 8)[0])
            elif btype == 6:
                iface, _, _, cap = struct.unpack_from(e + "IIII", data, off + 8)
                yield links[iface] if iface < len(links) else 1, data[off + 28:off + 28 + cap]
            off += blen
    else:
        raise SystemExit("not a pcap/pcapng file")


def tcp_segments(path: str, port: int):
    for lt, pkt in read_packets(path):
        if lt == 1:          # Ethernet
            et, l2 = struct.unpack_from(">H", pkt, 12)[0], 14
            while et == 0x8100:
                et, l2 = struct.unpack_from(">H", pkt, l2 + 2)[0], l2 + 4
        elif lt == 113:      # Linux cooked
            et, l2 = struct.unpack_from(">H", pkt, 14)[0], 16
        elif lt in (101, 228):
            et, l2 = 0x0800, 0
        else:
            continue
        if et != 0x0800 or len(pkt) < l2 + 20:
            continue
        ihl = (pkt[l2] & 0x0F) * 4
        if pkt[l2 + 9] != 6:
            continue
        tot = struct.unpack_from(">H", pkt, l2 + 2)[0]
        src, dst = pkt[l2 + 12:l2 + 16], pkt[l2 + 16:l2 + 20]
        t = l2 + ihl
        sport, dport, seq = struct.unpack_from(">HHI", pkt, t)
        doff = (pkt[t + 12] >> 4) * 4
        payload = pkt[t + doff:l2 + tot]
        if port not in (sport, dport) or not payload:
            continue
        s = ".".join(map(str, src)) + f":{sport}"
        d = ".".join(map(str, dst)) + f":{dport}"
        yield s, d, seq, payload


def reassemble(segs):
    """flow (src,dst) -> bytes, in sequence order (duplicates dropped)."""
    flows = defaultdict(dict)
    for s, d, seq, p in segs:
        flows[(s, d)].setdefault(seq, p)
    out = {}
    for k, parts in flows.items():
        buf, nxt = bytearray(), None
        for seq in sorted(parts):
            p = parts[seq]
            if nxt is not None and seq < nxt:
                p = p[nxt - seq:]
            buf += p
            nxt = seq + len(parts[seq]) if nxt is None or seq + len(parts[seq]) > nxt else nxt
        out[k] = bytes(buf)
    return out


# ---------------------------------------------------------------- decoding
def decode_camera(data: bytes, password: str):
    cr = Crypter()
    dec = bc.Decoder(cr)
    dec.feed(data)
    nonce, neg, msgs = "", Enc.NONE, []
    for m in dec.messages():
        msgs.append(m)
        if m.msg_id == 1 and (m.response >> 8) == 0xDD:
            nonce = xml_value(m.payload, "nonce") or ""
            neg = Enc(m.response & 0xFF)
            cr.set(Enc.BC if neg != Enc.NONE else Enc.NONE)
        elif m.msg_id == 1 and m.response == 200:
            cr.set(neg, make_aes_key(nonce, password)
                   if neg.rank >= Enc.AES.rank else None)
    return msgs, nonce, neg


def decode_nvr(data: bytes, password: str, nonce: str, neg: Enc):
    cr = Crypter()
    dec = bc.Decoder(cr)
    dec.feed(data)
    msgs = []
    for m in dec.messages():
        msgs.append(m)
        if m.msg_id == 1 and m.cls == bc.CLASS_LEGACY:
            cr.set(Enc.BC if neg != Enc.NONE else Enc.NONE)
        elif m.msg_id == 1:
            cr.set(neg, make_aes_key(nonce, password)
                   if neg.rank >= Enc.AES.rank else None)
    return msgs


def show(prefix: str, m: bc.BcMsg, maxlen: int) -> None:
    print(f"{prefix} id={m.msg_id} ch={m.channel} st={m.stream_type} num={m.msg_num} "
          f"resp={m.response:#x} cls={m.cls:#06x} ext={len(m.ext)} pay={len(m.payload)}")
    if m.ext:
        print("   ext: " + m.ext_text.strip().replace("\n", "\n        "))
    if m.payload and not m.binary:
        t = m.payload_text.strip()
        print("   " + (t if len(t) <= maxlen else t[:maxlen] + " ...").replace("\n", "\n   "))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pcap")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("--templates", metavar="DIR", help="write camera replies as DIR/<id>.xml")
    ap.add_argument("--max", type=int, default=4000, help="truncate printed XML")
    a = ap.parse_args()

    flows = reassemble(tcp_segments(a.pcap, a.port))
    seen_nvr: dict[int, int] = defaultdict(int)
    replied: dict[int, int] = {}
    pairs = [(k, flows.get((k[1], k[0]), b"")) for k in flows if k[0].endswith(f":{a.port}")]
    for (cam, nvr), nvr_data in pairs:
        cam_data = flows[(cam, nvr)]
        print(f"\n===== connection {nvr} -> {cam}  ({len(nvr_data)} / {len(cam_data)} bytes)")
        cmsgs, nonce, neg = decode_camera(cam_data, a.password)
        nmsgs = decode_nvr(nvr_data, a.password, nonce, neg)
        print(f"nonce={nonce} encryption={neg.name}")
        bin_skip = 0
        for m in nmsgs:
            seen_nvr[m.msg_id] += 1
            show("NVR>", m, a.max)
        for m in cmsgs:
            if m.binary and m.msg_id == 3:
                bin_skip += 1
                continue
            show("CAM<", m, a.max)
            if m.response == 200 and m.payload and not m.binary and m.msg_id not in (1, 3, 33):
                replied.setdefault(m.msg_id, m.response)
                if a.templates:
                    os.makedirs(a.templates, exist_ok=True)
                    fn = os.path.join(a.templates, f"{m.msg_id}.xml")
                    if not os.path.exists(fn):
                        open(fn, "wb").write(m.payload)
            if m.msg_id == 1 and m.response == 200 and m.payload and a.templates:
                os.makedirs(a.templates, exist_ok=True)
                fn = os.path.join(a.templates, "login_deviceinfo.xml")
                if not os.path.exists(fn):
                    open(fn, "wb").write(m.payload)
        if bin_skip:
            print(f"(skipped {bin_skip} binary video messages)")

    print("\n===== summary: msg ids requested by the NVR")
    for mid in sorted(seen_nvr):
        print(f"  {mid:5d}  x{seen_nvr[mid]:<4d} {'template captured' if mid in replied else ''}")


if __name__ == "__main__":
    main()
