#!/usr/bin/env python3
"""Recording TCP relay between a Reolink client (App / NVR) and a REAL camera.

Point the App at this relay (add device by IP, port = --listen port), use the
pages you want to learn, then decrypt the capture with bc_dump.py:

    bc_relay.py 192.168.1.50 --listen 0.0.0.0:9001 --out app.pcap
    bc_dump.py app.pcap -p <camera password> --templates captured/

No password is needed here: the relay only copies bytes. The pcap uses fake
Ethernet/IPv4/TCP headers with the camera side on port 9000, which is what
bc_dump.py expects. Every client connection gets its own TCP flow.
"""
from __future__ import annotations

import argparse
import asyncio
import socket
import struct
import time


class PcapWriter:
    def __init__(self, path: str) -> None:
        self.f = open(path, "wb")
        self.f.write(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
        self.f.flush()

    def segment(self, src: tuple, dst: tuple, seq: int, data: bytes) -> None:
        for off in range(0, len(data), 1400):
            seg = data[off:off + 1400]
            tcp = struct.pack(">HHIIBBHHH", src[1], dst[1], (seq + off) & 0xFFFFFFFF, 0, 5 << 4, 0x18,
                              65535, 0, 0)
            ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp) + len(seg), 0, 0, 64, 6, 0,
                             socket.inet_aton(src[0]), socket.inet_aton(dst[0]))
            pkt = b"\x00" * 12 + b"\x08\x00" + ip + tcp + seg
            t = time.time()
            self.f.write(struct.pack("<IIII", int(t), int(t % 1 * 1e6), len(pkt), len(pkt)) + pkt)
        self.f.flush()


async def main_async(a) -> None:
    pcap = PcapWriter(a.out)
    host, port = a.listen.rsplit(":", 1)
    n = 0

    async def on_conn(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        nonlocal n
        n += 1
        peer = w.get_extra_info("peername")
        cli = (peer[0], peer[1])
        cam = (socket.gethostbyname(a.camera), 9000)   # always 9000 in the pcap
        try:
            ur, uw = await asyncio.open_connection(a.camera, a.port)
        except OSError as e:
            print(f"[{n}] {cli[0]}: cannot reach camera: {e}", flush=True)
            w.close()
            return
        print(f"[{n}] {cli[0]}:{cli[1]} connected", flush=True)
        seq = {True: 1, False: 1}
        total = {True: 0, False: 0}

        async def pump(src, dst, from_cli: bool) -> None:
            try:
                while data := await src.read(65536):
                    pcap.segment(cli if from_cli else cam, cam if from_cli else cli, seq[from_cli], data)
                    seq[from_cli] += len(data)
                    total[from_cli] += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, OSError):
                pass
            finally:
                dst.close()

        await asyncio.gather(pump(r, uw, True), pump(ur, w, False))
        print(f"[{n}] {cli[0]}:{cli[1]} closed (client {total[True]} B, camera {total[False]} B)", flush=True)

    srv = await asyncio.start_server(on_conn, host or "0.0.0.0", int(port))
    print(f"relay {a.listen} -> {a.camera}:{a.port}, writing {a.out} (Ctrl+C to stop)", flush=True)
    async with srv:
        await srv.serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("camera", help="real Reolink camera IP")
    ap.add_argument("--port", type=int, default=9000, help="camera port")
    ap.add_argument("--listen", default="0.0.0.0:9001")
    ap.add_argument("--out", default="relay.pcap")
    a = ap.parse_args()
    try:
        asyncio.run(main_async(a))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
