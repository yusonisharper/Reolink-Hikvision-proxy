"""Baichuan (Reolink) payload encryption.

Three schemes are negotiated during login:

* none      - plaintext
* bc        - "BCEncrypt", a fixed 8-byte XOR key mixed with the header's
              channel byte (the so-called "encryption offset")
* aes       - AES-128-CFB (128-bit segments), IV "0123456789abcdef",
              key = first 16 chars of uppercase-hex MD5("<nonce>-<password>").
              Every section (extension, payload) is encrypted on its own,
              i.e. the cipher restarts from the IV for each section.
              Binary payloads (BcMedia video, JPEG) stay in clear.
* full_aes  - same as aes, but binary payloads are AES encrypted too; the
              message's extension carries <encryptLen>N</encryptLen> = number
              of leading payload bytes that are encrypted (rest is clear).

Reference: neolink crates/core/src/bc/crypto.rs, reolink_aio baichuan.py
"""
from __future__ import annotations

import hashlib
from enum import IntEnum

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms

try:  # cryptography >= 47 moved CFB to "decrepit"
    from cryptography.hazmat.decrepit.ciphers.modes import CFB
except ImportError:  # pragma: no cover
    from cryptography.hazmat.primitives.ciphers.modes import CFB

XML_KEY = bytes([0x1F, 0x2D, 0x3C, 0x4B, 0x5A, 0x69, 0x78, 0xFF])
AES_IV = b"0123456789abcdef"


class Enc(IntEnum):
    """Low byte of the login response code (0xddXX)."""

    NONE = 0x00
    BC = 0x01
    AES = 0x02
    FULL_AES = 0x12

    @property
    def rank(self) -> int:
        return {Enc.NONE: 0, Enc.BC: 1, Enc.AES: 2, Enc.FULL_AES: 3}[self]

    @classmethod
    def from_name(cls, name: str) -> "Enc":
        return {"none": cls.NONE, "bc": cls.BC, "aes": cls.AES, "full_aes": cls.FULL_AES}[name]

    @classmethod
    def from_offer(cls, byte: int) -> "Enc":
        """Map the client's offered max (low byte of 0xdcXX) to an Enc."""
        try:
            return cls(byte)
        except ValueError:
            # Unknown offer: be conservative.
            return cls.BC if byte else cls.NONE


def bc_xor(offset: int, data: bytes) -> bytes:
    """BCEncrypt (symmetric)."""
    off = offset & 0xFF
    start = offset % 8
    return bytes(b ^ XML_KEY[(start + i) % 8] ^ off for i, b in enumerate(data))


def md5_hex_upper(text: str) -> str:
    return hashlib.md5(text.encode("utf-8")).hexdigest().upper()


def login_hash(value: str, nonce: str) -> str:
    """Hash used in <LoginUser>: MD5(value+nonce), uppercase hex, 31 chars."""
    return md5_hex_upper(value + nonce)[:31]


def make_aes_key(nonce: str, password: str) -> bytes:
    return md5_hex_upper(f"{nonce}-{password}")[:16].encode("ascii")


def _aes(key: bytes):
    return Cipher(algorithms.AES(key), CFB(AES_IV))


def aes_encrypt(key: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    enc = _aes(key).encryptor()
    return enc.update(data) + enc.finalize()


def aes_decrypt(key: bytes, data: bytes) -> bytes:
    if not data:
        return b""
    dec = _aes(key).decryptor()
    return dec.update(data) + dec.finalize()


class Crypter:
    """Per-connection encryption state."""

    def __init__(self) -> None:
        self.mode: Enc = Enc.NONE
        self.aes_key: bytes | None = None

    def set(self, mode: Enc, aes_key: bytes | None = None) -> None:
        self.mode = mode
        self.aes_key = aes_key

    def _effective(self, msg_id: int) -> Enc:
        # During login (msg 1) at most BCEncrypt is used, even if AES was agreed.
        if msg_id == 1 and self.mode.rank > Enc.BC.rank:
            return Enc.BC
        return self.mode

    @property
    def full_aes(self) -> bool:
        return self.mode == Enc.FULL_AES and self.aes_key is not None

    def encrypt(self, msg_id: int, offset: int, data: bytes) -> bytes:
        mode = self._effective(msg_id)
        if mode == Enc.NONE or not data:
            return data
        if mode == Enc.BC:
            return bc_xor(offset, data)
        assert self.aes_key is not None
        return aes_encrypt(self.aes_key, data)

    def decrypt(self, msg_id: int, offset: int, data: bytes) -> bytes:
        mode = self._effective(msg_id)
        if mode == Enc.NONE or not data:
            return data
        if mode == Enc.BC:
            return bc_xor(offset, data)
        assert self.aes_key is not None
        return aes_decrypt(self.aes_key, data)
