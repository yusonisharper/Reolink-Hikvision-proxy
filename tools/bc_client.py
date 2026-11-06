#!/usr/bin/env python3
"""Minimal Baichuan client.

Uses:
  * test the proxy without a Reolink NVR
  * dump replies of a REAL Reolink camera into template files

Examples:
  bc_client.py 192.168.1.50 -p pass --alarms                 # print msg 33 events
  bc_client.py 192.168.1.50 -p pass --get 199 527 529 531    # print replies
  bc_client.py 192.168.1.50 -p pass --dump-templates captured/  --get 58 80 104 114 146 151 199 299 527 529 531 44 56
  bc_client.py 127.0.0.1 -p pass --preview subStream --out sub.h264 --seconds 10
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from bcproxy import bc, bcmedia  # noqa: E402
from bcproxy.bc import BcMsg, body_xml, ext_xml, xml_value  # noqa: E402
from bcproxy.crypto import Crypter, Enc, login_hash, make_aes_key  # noqa: E402

CHANNEL_EXT_IDS = {26, 44, 56, 109, 146, 151, 199, 299, 527, 529, 531, 132}


class Client:
    def __init__(self, host: str, port: int, user: str, password: str, offer: int = 0x12,
                 magic: int = bc.MAGIC) -> None:
        self.host, self.port, self.user, self.password, self.offer = host, port, user, password, offer
        self.magic = magic
        self.crypter = Crypter()
        self.decoder = bc.Decoder(self.crypter)
        self.num = 0
        self.inbox: asyncio.Queue[BcMsg] = asyncio.Queue()
        self.subs: dict[tuple[int, int], asyncio.Queue] = {}
        self.events: asyncio.Queue[BcMsg] = asyncio.Queue()

    async def connect(self) -> None:
        self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
        self._rx = asyncio.create_task(self._read())

    async def _read(self) -> None:
        while True:
            data = await self.reader.read(65536)
            if not data:
                break
            self.decoder.feed(data)
            for m in self.decoder.messages():
                if m.msg_id == bc.MSG_ALARM_EVENT:
                    await self.events.put(m)
                q = self.subs.get((m.msg_id, m.msg_num))
                if q:
                    await q.put(m)
                # login: switch keys before decoding the next message
                if m.msg_id == 1 and (m.response >> 8) == 0xDD:
                    self.nonce = xml_value(m.payload, "nonce") or ""
                    self.neg = Enc(m.response & 0xFF)
                    self.crypter.set(Enc.BC if self.neg != Enc.NONE else Enc.NONE)
                elif m.msg_id == 1 and m.response == 200:
                    if self.neg in (Enc.AES, Enc.FULL_AES):
                        self.crypter.set(self.neg, make_aes_key(self.nonce, self.password))
                    else:
                        self.crypter.set(self.neg)
        for q in self.subs.values():
            await q.put(None)

    def _next_num(self) -> int:
        self.num = (self.num + 1) & 0xFFFF
        return self.num

    async def request(self, msg_id: int, ext: bytes = b"", payload: bytes = b"",
                      cls: int = bc.CLASS_MODERN_REQ, response: int = 0, num: int | None = None,
                      wait: bool = True, timeout: float = 5) -> BcMsg | None:
        num = self._next_num() if num is None else num
        q = self.subs.setdefault((msg_id, num), asyncio.Queue())
        m = BcMsg(msg_id, 0, 0, num, response, cls, ext, payload)
        self.writer.write(bc.encode(m, self.crypter, self.magic))
        await self.writer.drain()
        if not wait:
            return None
        return await asyncio.wait_for(q.get(), timeout)

    async def login(self) -> BcMsg:
        num = self._next_num()
        r = await self.request(1, cls=bc.CLASS_LEGACY, response=0xDC00 | self.offer, num=num)
        assert r and (r.response >> 8) == 0xDD, f"unexpected login reply {r}"
        login = body_xml(
            f'<LoginUser version="1.1">\n<userName>{login_hash(self.user, self.nonce)}</userName>\n'
            f"<password>{login_hash(self.password, self.nonce)}</password>\n<userVer>1</userVer>\n</LoginUser>\n"
            f'<LoginNet version="1.1">\n<type>LAN</type>\n<udpPort>0</udpPort>\n</LoginNet>')
        r = await self.request(1, payload=login, num=num)
        if not r or r.response != 200:
            raise SystemExit(f"login failed: {r and r.response}")
        await asyncio.sleep(0.05)  # let reader switch keys
        return r


async def amain(a) -> None:
    c = Client(a.host, a.port, a.user, a.password, int(a.offer, 0),
               bc.MAGIC_REV if a.nvr_magic else bc.MAGIC)
    await c.connect()
    r = await c.login()
    print(f"login OK, encryption={c.crypter.mode.name}")
    if a.verbose:
        print(r.payload_text)

    for mid in a.get or []:
        ext = ext_xml(channelId=0) if mid in CHANNEL_EXT_IDS else b""
        if mid in (58, 151):
            ext = ext_xml(userName=a.user, token="system, network, alarm, record, video, image" if mid == 151 else None)
        try:
            rep = await c.request(mid, ext=ext)
        except asyncio.TimeoutError:
            print(f"--- {mid}: timeout")
            continue
        print(f"--- {mid}: response {rep.response}\n{rep.payload_text}")
        if a.dump_templates and rep.response == 200 and rep.payload:
            os.makedirs(a.dump_templates, exist_ok=True)
            with open(os.path.join(a.dump_templates, f"{mid}.xml"), "wb") as f:
                f.write(rep.payload)

    if a.snap:
        rep = await c.request(109, ext=ext_xml(channelId=0), payload=body_xml(
            '<Snap version="1.1">\n<channelId>0</channelId>\n<logicChannel>0</logicChannel>\n'
            '<time>0</time>\n<fullFrame>0</fullFrame>\n<streamType>main</streamType>\n</Snap>'))
        size = int(xml_value(rep.payload, "pictureSize") or 0)
        buf = b""
        q = c.subs[(109, rep.msg_num)]
        while True:
            m = await asyncio.wait_for(q.get(), 10)
            buf += m.payload
            if m.response == 201:
                break
        open(a.snap, "wb").write(buf)
        print(f"snapshot {len(buf)}/{size} bytes -> {a.snap}")

    if a.preview:
        rep = await c.request(3, ext=ext_xml(channelId=0), payload=body_xml(
            f'<Preview version="1.1">\n<channelId>0</channelId>\n<handle>0</handle>\n'
            f"<streamType>{a.preview}</streamType>\n</Preview>"))
        print(f"preview response {rep.response}")
        q = c.subs[(3, rep.msg_num)]
        reader = bcmedia.BcMediaReader()
        frames = {"I": 0, "P": 0, "audio": 0}
        out = open(a.out, "wb") if a.out else None
        end = time.monotonic() + a.seconds
        pending = [rep]
        while time.monotonic() < end:
            m = pending.pop() if pending else await asyncio.wait_for(q.get(), 10)
            for kind, meta, data in reader.feed(m.payload):
                if kind == "info":
                    print("info", meta)
                elif kind in frames:
                    frames[kind] += 1
                    if out and kind != "audio":
                        out.write(data)
        print(f"frames: {frames}")
        await c.request(4, ext=ext_xml(channelId=0), payload=body_xml(
            '<Preview version="1.1">\n<channelId>0</channelId>\n<handle>0</handle>\n</Preview>'))

    if a.alarms:
        rep = await c.request(31)
        print(f"alarm subscribe response {rep.response}; waiting for msg 33 (Ctrl-C to stop)")
        end = time.monotonic() + (a.seconds if a.seconds else 1e9)
        while time.monotonic() < end:
            try:
                m = await asyncio.wait_for(c.events.get(), 1)
            except asyncio.TimeoutError:
                continue
            print(time.strftime("%H:%M:%S"), m.payload_text.strip(), flush=True)
    c.writer.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("host")
    ap.add_argument("--port", type=int, default=9000)
    ap.add_argument("-u", "--user", default="admin")
    ap.add_argument("-p", "--password", default="")
    ap.add_argument("--offer", default="0x12", help="max encryption offered: 0x00 0x01 0x02 0x12")
    ap.add_argument("--nvr-magic", action="store_true",
                    help="use the NVR header magic 0x0fedcba0 (behave like a Reolink NVR)")
    ap.add_argument("--get", type=int, nargs="*", help="message ids to request")
    ap.add_argument("--dump-templates", metavar="DIR", help="save 200-OK payloads as <id>.xml")
    ap.add_argument("--alarms", action="store_true", help="subscribe (msg 31) and print msg 33")
    ap.add_argument("--preview", choices=["mainStream", "subStream"])
    ap.add_argument("--out", help="write received Annex-B video here")
    ap.add_argument("--snap", help="request a snapshot and save the JPEG here")
    ap.add_argument("--seconds", type=float, default=10)
    ap.add_argument("-v", "--verbose", action="store_true")
    asyncio.run(amain(ap.parse_args()))


if __name__ == "__main__":
    main()
