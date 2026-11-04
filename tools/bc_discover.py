#!/usr/bin/env python3
"""Reolink LAN search: broadcast aa aa 00 00 to UDP 2000 and list the replies.

Finds real Reolink cameras/NVRs and the proxy's virtual cameras alike.

Examples:
  bc_discover.py                          # all interfaces, 3 s
  bc_discover.py --bind 192.168.1.10      # send from one interface
  bc_discover.py --to 192.168.1.201 --raw # ask one host, print reply hex
"""
from __future__ import annotations

import argparse
import os
import select
import socket
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bcproxy import discovery  # noqa: E402


def default_targets(bind: str) -> list[str]:
    """With several NICs 255.255.255.255 leaves through one of them only, so
    also send to each local /24's directed broadcast."""
    addrs = [bind] if bind != "0.0.0.0" else socket.gethostbyname_ex(socket.gethostname())[2]
    return ["255.255.255.255"] + sorted({ip.rsplit(".", 1)[0] + ".255" for ip in addrs
                                         if not ip.startswith("127.")})


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bind", default="0.0.0.0", help="local address to send from")
    ap.add_argument("--to", action="append",
                    help="destination (default: 255.255.255.255 + x.y.z.255 of each local address); repeatable")
    ap.add_argument("--seconds", type=float, default=3)
    ap.add_argument("--raw", action="store_true", help="also print reply hex")
    a = ap.parse_args()

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        s.bind((a.bind, discovery.REPLY_PORT))   # devices always answer to UDP 3000
    except OSError as e:
        sys.exit(f"cannot bind udp {a.bind}:{discovery.REPLY_PORT} ({e}); "
                 "close the Reolink Client or anything else using it")
    for dst in a.to or default_targets(a.bind):
        try:
            s.sendto(discovery.REQUEST, (dst, discovery.PORT))
        except OSError as e:
            print(f"send to {dst}: {e}", file=sys.stderr)

    seen = set()
    end = time.monotonic() + a.seconds
    while (left := end - time.monotonic()) > 0:
        if not select.select([s], [], [], left)[0]:
            break
        data, addr = s.recvfrom(4096)
        if data in seen:
            continue
        seen.add(data)
        d = discovery.parse_reply(data)
        if d is None:
            print(f"{addr[0]}:{addr[1]}  ? {len(data)} bytes: {data[:32].hex()}")
            continue
        print(f"{d['ip']:>15}:{d['port']:<5} {d['type']:3} {d['mac']}  {d['uid'] or '-':20} {d['name']}"
              f"{'  (from ' + addr[0] + ')' if addr[0] != d['ip'] else ''}")
        if a.raw:
            print("   ", data.hex())
    print(f"{len(seen)} device(s)")


if __name__ == "__main__":
    main()
