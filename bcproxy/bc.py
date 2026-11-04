"""Baichuan TCP framing.

Header (little endian):

    u32 magic        0x0abcdef0
    u32 msg_id
    u32 body_len     (extension + payload)
    u8  channel      also used as the BCEncrypt "offset"
    u8  stream_type
    u16 msg_num      echoed back in replies
    u16 response     0 in requests, 200 OK, 400 error, 0xdcXX/0xddXX in login
    u16 class        0x6514 legacy (20B hdr), 0x6614 (20B), 0x6414 / 0x0000 (24B)
    u32 payload_off  only for classes 0x6414 and 0x0000 (= extension length)
"""
from __future__ import annotations

import re
import struct
from dataclasses import dataclass, field

from .crypto import Crypter

MAGIC = 0x0ABCDEF0          # client (app / neolink) <-> camera
MAGIC_REV = 0x0FEDCBA0      # Reolink NVR <-> camera; the camera must answer with the same magic
HDR = struct.Struct("<IIIBBHHH")

CLASS_LEGACY = 0x6514
CLASS_MODERN_NOOFF = 0x6614
CLASS_MODERN_REQ = 0x6414
CLASS_MODERN = 0x0000

XML_DECL = '<?xml version="1.0" encoding="UTF-8" ?>\n'

# Well known message ids
MSG_LOGIN = 1
MSG_LOGOUT = 2
MSG_VIDEO = 3
MSG_VIDEO_STOP = 4
MSG_ALARM_START = 31
MSG_ALARM_EVENT = 33
MSG_PING = 93
MSG_SNAP = 109


def has_offset(cls: int) -> bool:
    # Replay data frames (msg 381) carry the session <seq> (u32) in the
    # response/class bytes, with a 24-byte header: anything unknown has one.
    return cls not in (CLASS_LEGACY, CLASS_MODERN_NOOFF)


@dataclass
class BcMsg:
    msg_id: int
    channel: int = 0
    stream_type: int = 0
    msg_num: int = 0
    response: int = 0
    cls: int = CLASS_MODERN
    ext: bytes = b""          # plaintext extension XML
    payload: bytes = b""      # plaintext XML or binary
    binary: bool = False      # payload is binary (encrypted only with full AES)
    raw_body: bytes = field(default=b"", repr=False)

    # ------------------------------------------------------------------ helpers
    @property
    def payload_text(self) -> str:
        if self.binary:
            return f"<{len(self.payload)} bytes binary>"
        return self.payload.decode("utf-8", "replace")

    @property
    def ext_text(self) -> str:
        return self.ext.decode("utf-8", "replace")

    def reply(self, response: int = 200, ext: bytes = b"", payload: bytes = b"",
              binary: bool = False, cls: int = CLASS_MODERN) -> "BcMsg":
        """Build a reply that echoes channel/stream/msg_num."""
        return BcMsg(self.msg_id, self.channel, self.stream_type, self.msg_num,
                     response, cls, ext, payload, binary)


def body_xml(inner: str) -> bytes:
    return (XML_DECL + "<body>\n" + inner.strip() + "\n</body>\n").encode()


def ext_xml(**fields) -> bytes:
    inner = "".join(f"<{k}>{v}</{k}>\n" for k, v in fields.items() if v is not None)
    return (XML_DECL + '<Extension version="1.1">\n' + inner + "</Extension>\n").encode()


def add_ext_field(ext: bytes, tag: str, value) -> bytes:
    """Add/replace <tag> in an Extension XML (create one if empty)."""
    if not ext:
        return ext_xml(**{tag: value})
    text = re.sub(rf"<{tag}>.*?</{tag}>\n?", "", ext.decode("utf-8", "replace"), flags=re.S)
    return text.replace("</Extension>", f"<{tag}>{value}</{tag}>\n</Extension>").encode()


def xml_value(text: bytes | str, tag: str) -> str | None:
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    m = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return m.group(1).strip() if m else None


def encode(msg: BcMsg, crypter: Crypter, magic: int = MAGIC) -> bytes:
    off = msg.channel
    if msg.cls == CLASS_LEGACY:
        body = msg.payload
        ext_len = None
    else:
        ext_plain = msg.ext
        if msg.binary and msg.payload and crypter.full_aes and msg.msg_id != MSG_LOGIN:
            # Full AES: binary payload encrypted, length announced in the extension.
            # (Verified with a real Reolink NVR + app. Real cameras additionally send
            # checkPos/checkValue and encrypt only 1024 bytes; not needed so far.)
            ext_plain = add_ext_field(ext_plain, "encryptLen", len(msg.payload))
            pay = crypter.encrypt(msg.msg_id, off, msg.payload)
        elif msg.binary:
            pay = msg.payload  # media/jpeg in clear (aes / bc / none)
        else:
            pay = crypter.encrypt(msg.msg_id, off, msg.payload)
        ext = crypter.encrypt(msg.msg_id, off, ext_plain)
        body = ext + pay
        ext_len = len(ext) if has_offset(msg.cls) else None
    hdr = HDR.pack(magic, msg.msg_id, len(body), msg.channel, msg.stream_type,
                   msg.msg_num, msg.response, msg.cls)
    if ext_len is not None:
        hdr += struct.pack("<I", ext_len)
    return hdr + body


class Decoder:
    """Incremental decoder for one direction of a Baichuan TCP stream."""

    def __init__(self, crypter: Crypter) -> None:
        self.crypter = crypter
        self.buf = bytearray()
        # Binary mode is per request. Key on all 4 id bytes (ch, stream, num): the
        # Reolink app keeps num=0 and counts in the first byte instead.
        self.binary_ids: set[tuple[int, int, int]] = set()
        self.skipped = 0          # bytes thrown away while resyncing on magic
        self.magic: int | None = None   # magic of the first valid message (echo it back)

    def feed(self, data: bytes) -> None:
        self.buf += data

    def next(self) -> BcMsg | None:
        """Decode one message. Decode lazily so a key change (login) applies to
        the messages that follow it in the same TCP read."""
        return self._one()

    def messages(self):
        while (m := self._one()) is not None:
            yield m

    def _one(self) -> BcMsg | None:
        if len(self.buf) < 20:
            return None
        magic, msg_id, body_len, ch, st, num, resp, cls = HDR.unpack_from(self.buf, 0)
        if magic not in (MAGIC, MAGIC_REV):
            # resync on next magic
            hits = [i for i in (self.buf.find(struct.pack("<I", mg), 1) for mg in (MAGIC, MAGIC_REV))
                    if i >= 0]
            idx = min(hits) if hits else -1
            if idx < 0:
                self.skipped += max(len(self.buf) - 3, 0)
                del self.buf[:-3]
                return None
            self.skipped += idx
            del self.buf[:idx]
            return self._one()
        if self.magic is None:
            self.magic = magic
        hlen = 24 if has_offset(cls) else 20
        if len(self.buf) < hlen + body_len:
            return None
        ext_len = struct.unpack_from("<I", self.buf, 20)[0] if hlen == 24 else 0
        body = bytes(self.buf[hlen:hlen + body_len])
        del self.buf[:hlen + body_len]

        msg = BcMsg(msg_id, ch, st, num, resp, cls, raw_body=body)
        if cls == CLASS_LEGACY:
            msg.payload = body
            return msg
        ext_raw, pay_raw = body[:ext_len], body[ext_len:]
        if msg_id == MSG_LOGIN and (resp >> 8) == 0xDD:
            # Encryption negotiation reply: payload is BCEncrypted unless "none"
            from .crypto import bc_xor
            msg.payload = bc_xor(ch, pay_raw) if (resp & 0xFF) else pay_raw
            return msg
        msg.ext = self.crypter.decrypt(msg_id, ch, ext_raw)
        bd = xml_value(msg.ext, "binaryData")
        rid = (ch, st, num)
        if bd == "1":
            self.binary_ids.add(rid)
        elif bd == "0":
            self.binary_ids.discard(rid)
        if rid in self.binary_ids and msg_id != MSG_LOGIN:
            msg.binary = True
            enc_len = xml_value(msg.ext, "encryptLen") or xml_value(msg.ext, "EncryptLen")  # real cams: "E"
            if enc_len is not None and self.crypter.full_aes:
                n = int(enc_len)
                msg.payload = self.crypter.decrypt(msg_id, ch, pay_raw[:n]) + pay_raw[n:]
            else:
                msg.payload = pay_raw
        else:
            msg.payload = self.crypter.decrypt(msg_id, ch, pay_raw)
        return msg
