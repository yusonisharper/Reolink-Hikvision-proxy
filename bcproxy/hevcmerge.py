"""Merge the tile slices of an H.265 picture into one slice, without re-encoding (native/hevcmerge.c).

Hikvision DS-2CD2347G1-L and DS-2CD2347G2-LU send their main stream as 3 tiles, one slice each.
Reolink's mp4 writer (phone app, PC client, NVR backup) keeps at most 600 pieces per GOP, 2 per NAL
unit, so with 3 slices a GOP may have only 100 pictures, and StreamSource._guard_gop has to ask the
camera for keyframes far more often than H.265+ wants. As one slice the limit is 300 pictures.

The C library entropy-decodes each tile to rewrite its last bits (see the comment at its top); the
pictures decode to exactly the same pixels. Anything it does not support is passed through unchanged.
It is built per CPU by native/build.sh; without a library for this machine nothing is merged.
"""
from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import platform
from concurrent.futures import ThreadPoolExecutor

from .bcmedia import AccessUnit

log = logging.getLogger(__name__)

REASONS = {
    1: "single slice", 2: "out of memory", 3: "no SPS/PPS yet", 4: "SPS not supported",
    5: "PPS not supported", 6: "slice header not supported", 7: "slice headers differ",
    8: "slices are not whole tiles", 9: "merging would change in-loop filtering",
    10: "slice data did not decode", 11: "tile end could not be rewritten",
    12: "merged slice did not decode", 13: "output buffer too small",
}
NO_PARAMS = 3

enabled = True            # config: merge_tiles
_lib: ctypes.CDLL | None = None
_lib_error: str | None = None


def library_path(machine: str | None = None) -> str:
    machine = (machine or platform.machine()).lower()
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return os.path.join(os.path.dirname(__file__), "native", f"libhevcmerge-{machine}.so")


def _load() -> ctypes.CDLL | None:
    global _lib, _lib_error
    if _lib is not None or _lib_error is not None:
        return _lib
    path = library_path()
    try:
        lib = ctypes.CDLL(path)
    except OSError as e:
        _lib_error = str(e)
        log.info("H.265 tile merging off: %s not loaded (%s); build it with bcproxy/native/build.sh", path, e)
        return None
    lib.hm_new.restype = ctypes.c_void_p
    lib.hm_new.argtypes = []
    lib.hm_free.restype = None
    lib.hm_free.argtypes = [ctypes.c_void_p]
    lib.hm_merge.restype = ctypes.c_long
    lib.hm_merge.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p,
                             ctypes.c_size_t, ctypes.c_int]
    _lib = lib
    return lib


def available() -> bool:
    return enabled and _load() is not None


class TileMerger:
    """One per stream: the library keeps the stream's SPS/PPS between pictures."""

    def __init__(self, name: str = "", verify: bool = False) -> None:
        self.name = name
        self.verify = verify
        self.merged = 0
        self.failed: dict[int, int] = {}
        self._lib = _load()
        self._h = self._lib.hm_new() if self._lib else None
        self._buf = ctypes.create_string_buffer(0)
        self._said_merging = False

    def __del__(self) -> None:
        if getattr(self, "_h", None) and self._lib:
            self._lib.hm_free(self._h)
            self._h = None

    def merge(self, au: bytes) -> bytes | None:
        """The access unit (Annex B, start codes) with its slices merged, or None to send it as it is."""
        if not self._h:
            return None
        cap = len(au) + 1024 + len(au) // 64
        if len(self._buf) < cap:
            self._buf = ctypes.create_string_buffer(cap * 2)
        r = self._lib.hm_merge(self._h, au, len(au), self._buf, len(self._buf), 1 if self.verify else 0)
        if r > 0:
            self.merged += 1
            if not self._said_merging:
                self._said_merging = True
                log.info("%s: merging the slices of each H.265 picture into one", self.name)
            return ctypes.string_at(self._buf, r)
        code = -r
        n = self.failed.get(code, 0) + 1
        self.failed[code] = n
        # a stream that starts without its parameter sets is normal; anything else is worth one line
        if code != NO_PARAMS and n == 1:
            log.warning("%s: H.265 picture sent with its slices unmerged: %s (logged once per reason)",
                        self.name, REASONS.get(code, code))
        return None


class StreamMerger:
    """Merges one stream's pictures on a thread of its own: the C call releases the GIL, so a big
    keyframe holds up neither the event loop nor the other cameras. Pictures come back in order."""

    def __init__(self, name: str) -> None:
        self.merger = TileMerger(name)
        self._pool = ThreadPoolExecutor(1, thread_name_prefix=f"merge-{name}")

    async def __call__(self, au: AccessUnit) -> AccessUnit:
        if au.slices <= 1:
            return au
        data = await asyncio.get_running_loop().run_in_executor(self._pool, self.merger.merge, au.data)
        return au if data is None else AccessUnit(data, au.keyframe, 1)

    def close(self) -> None:
        self._pool.shutdown(wait=False)


def stream_merger(name: str, codec: str) -> StreamMerger | None:
    return StreamMerger(name) if codec == "H265" and available() else None
