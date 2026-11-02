"""Server side of one Baichuan TCP connection (NVR -> virtual camera)."""
from __future__ import annotations

import asyncio
import logging
import os
import time
from string import Template

from . import bc, bcmedia
from . import settings as st
from .bc import BcMsg, body_xml, ext_xml, xml_value
from .camera import VirtualCamera
from .crypto import Crypter, Enc, login_hash, make_aes_key
from .events import Snapshot, alarm_event_xml
from .templates import SMART_MSG, smart_detect_xml

log = logging.getLogger("bc")

MEDIA_CHUNK = 32 * 1024
# Real cameras answer these with an empty 200 too (192: seen in a neolink capture;
# 287: TimeCfg write from the app - Hikvision keeps its own NTP time).
QUIET_OK = {192, 287}
SNAP_CHUNK = 32 * 1024


class BcSession:
    def __init__(self, cam: VirtualCamera, reader: asyncio.StreamReader,
                 writer: asyncio.StreamWriter, trace: bool = False) -> None:
        self.cam = cam
        self.reader, self.writer = reader, writer
        self.peer = writer.get_extra_info("peername")
        self.crypter = Crypter()
        self.decoder = bc.Decoder(self.crypter)
        self.nonce = ""
        self.negotiated = Enc.NONE
        self.logged_in = False
        self.username = ""
        self.alarm_req: BcMsg | None = None
        self.previews: dict[int, asyncio.Task] = {}
        self.tasks: set[asyncio.Task] = set()
        self.trace = trace
        self._wlock = asyncio.Lock()
        self.warned: set[int] = set()
        self.tag = f"{cam.cfg.name}<-{self.peer[0] if self.peer else '?'}"

    # ------------------------------------------------------------------- io
    async def send(self, msg: BcMsg) -> None:
        data = bc.encode(msg, self.crypter, self.decoder.magic or bc.MAGIC)
        if self.trace and (not msg.binary or msg.ext):
            log.debug("%s >> id=%d num=%d resp=%#x\n%s%s", self.tag, msg.msg_id, msg.msg_num,
                      msg.response, msg.ext_text, msg.payload_text)
        self.cam.tx.add(len(data))
        async with self._wlock:
            self.writer.write(data)
            await self.writer.drain()

    def push_alarm(self, snap: Snapshot) -> None:
        if not self.alarm_req or self.writer.is_closing():
            return
        payload = body_xml(alarm_event_xml(snap, channel=0,
                                           report_ai=bool(self.cam.cfg.ai_types),
                                           report_smart=bool(self.cam.smartai_bits)))
        r = self.alarm_req
        msg = BcMsg(bc.MSG_ALARM_EVENT, r.channel, r.stream_type, r.msg_num, 200, bc.CLASS_MODERN,
                    b"", payload)
        self._spawn(self.send(msg))

    def _spawn(self, coro) -> asyncio.Task:
        t = asyncio.create_task(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    async def run(self) -> None:
        log.info("%s: connected", self.tag)
        reason = "?"
        try:
            while True:
                data = await self.reader.read(65536)
                if not data:
                    reason = "peer closed (EOF)"
                    break
                if self.trace:
                    log.debug("%s raw << %d bytes: %s%s", self.tag, len(data), data[:256].hex(" "),
                              " ..." if len(data) > 256 else "")
                self.decoder.feed(data)
                for msg in self.decoder.messages():   # lazy: dispatch before decoding the next
                    if self.trace:
                        log.debug("%s << id=%d ch=%d num=%d resp=%#x cls=%#x\n%s%s", self.tag,
                                  msg.msg_id, msg.channel, msg.msg_num, msg.response, msg.cls,
                                  msg.ext_text, msg.payload_text)
                    await self.dispatch(msg)
        except (ConnectionError, asyncio.IncompleteReadError) as e:
            reason = f"{type(e).__name__}: {e}"
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except Exception:  # noqa: BLE001 - never die silently
            reason = "exception"
            log.exception("%s: session crashed", self.tag)
        finally:
            if self.decoder.buf:
                log.warning("%s: %d undecoded bytes left: %s", self.tag, len(self.decoder.buf),
                            bytes(self.decoder.buf[:128]).hex(" "))
            if self.decoder.skipped:
                log.warning("%s: decoder skipped %d bytes (bad magic)", self.tag, self.decoder.skipped)
            log.info("%s: closing: %s (logged_in=%s)", self.tag, reason, self.logged_in)
            self.cam.alarm_subs.discard(self)
            for t in list(self.previews.values()) + list(self.tasks):
                t.cancel()
            self.writer.close()
            log.info("%s: disconnected", self.tag)

    # -------------------------------------------------------------- dispatch
    async def dispatch(self, msg: BcMsg) -> None:
        if msg.msg_id == bc.MSG_LOGIN:
            await self.on_login(msg)
            return
        if not self.logged_in:
            await self.send(msg.reply(400))
            return
        handler = {
            bc.MSG_LOGOUT: self.on_logout,
            bc.MSG_VIDEO: self.on_preview,
            bc.MSG_VIDEO_STOP: self.on_preview_stop,
            bc.MSG_ALARM_START: self.on_alarm_start,
            bc.MSG_SNAP: self.on_snap,
        }.get(msg.msg_id)
        if handler:
            await handler(msg)
            return
        if msg.msg_id in SMART_MSG and not self.cam.templates.has(msg.msg_id):
            stype = SMART_MSG[msg.msg_id]
            zones = self.cam.cfg.smart_zones.get(stype)
            if zones:
                await self.send(msg.reply(200, payload=smart_detect_xml(stype, zones)))
            else:
                await self.send(msg.reply(400))
            return
        if msg.msg_id in self.cam.cfg.perf_msg_ids and not self.cam.templates.has(msg.msg_id):
            await self.send(msg.reply(200, payload=self.cam.performance_xml(msg.msg_id)))
            return
        is_write = bool(msg.payload) and not msg.binary
        if is_write:
            get_id = st.get_for_set(msg.msg_id, msg.payload, self._get_reply)
            if get_id is not None:
                await self.on_set(msg, get_id)
                return
        payload = self._get_reply(msg.msg_id)
        if payload is not None:
            await self.send(msg.reply(200, payload=payload))
            return
        if msg.msg_id in QUIET_OK:
            await self.send(msg.reply(200))
            return
        first = msg.msg_id not in self.warned
        self.warned.add(msg.msg_id)
        (log.warning if first else log.debug)("%s: unhandled msg %d (%s) -> %d\n%s%s", self.tag, msg.msg_id,
                    "write" if is_write else "read", 200 if is_write else self.cam.unknown_reply,
                    msg.ext_text, msg.payload_text)
        await self.send(msg.reply(200 if is_write else self.cam.unknown_reply))

    # ----------------------------------------------------------------- login
    async def on_login(self, msg: BcMsg) -> None:
        if msg.cls == bc.CLASS_LEGACY:
            offered = Enc.from_offer(msg.response & 0xFF) if (msg.response >> 8) == 0xDC else Enc.NONE
            wanted = Enc.from_name(self.cam.cfg.encryption)
            chosen = offered if offered.rank <= wanted.rank else wanted
            self.negotiated = chosen
            self.nonce = os.urandom(8).hex().upper()
            payload = body_xml(f'<Encryption version="1.1">\n<type>md5</type>\n'
                               f"<nonce>{self.nonce}</nonce>\n</Encryption>")
            # Reply is BCEncrypted (offset = channel) unless "none" was negotiated
            from .crypto import bc_xor
            body = bc_xor(msg.channel, payload) if chosen != Enc.NONE else payload
            reply = BcMsg(1, msg.channel, msg.stream_type, msg.msg_num, 0xDD00 | int(chosen),
                          self.cam.cfg.nego_class, payload=body)
            # encode() would encrypt again; write raw
            hdr = bc.HDR.pack(self.decoder.magic or bc.MAGIC, 1, len(body), msg.channel, msg.stream_type, msg.msg_num,
                              reply.response, reply.cls)
            if bc.has_offset(reply.cls):
                hdr += (0).to_bytes(4, "little")      # payload offset: no extension
            if self.trace:
                log.debug("%s >> id=1 ch=%d num=%d resp=%#x cls=%#x magic=%#x (negotiation)\n%s",
                          self.tag, msg.channel, msg.msg_num, reply.response, reply.cls,
                          self.decoder.magic or bc.MAGIC,
                          payload.decode("utf-8", "replace"))
            async with self._wlock:
                self.writer.write(hdr + body)
                await self.writer.drain()
            # the modern login that follows is BCEncrypted (or plain)
            self.crypter.set(Enc.BC if chosen != Enc.NONE else Enc.NONE)
            log.info("%s: offered=%s chosen=%s magic=%s", self.tag, offered.name, chosen.name,
                     "NVR(0x0fedcba0)" if self.decoder.magic == bc.MAGIC_REV else "client(0x0abcdef0)")
            return

        user_h = (xml_value(msg.payload, "userName") or "").rstrip("\x00")
        pass_h = (xml_value(msg.payload, "password") or "").rstrip("\x00")
        exp_u = login_hash(self.cam.cfg.username, self.nonce)
        exp_p = login_hash(self.cam.cfg.password, self.nonce)
        if not self.nonce or user_h[:31].upper() != exp_u or pass_h[:31].upper() != exp_p:
            log.warning("%s: login FAILED (bad credentials or no nonce)", self.tag)
            await self.send(msg.reply(400))
            return
        self.logged_in = True
        self.username = self.cam.cfg.username
        payload = self.cam.templates.render("login_deviceinfo", self.cam.template_vars(self.username))
        await self.send(msg.reply(200, payload=payload or b""))
        if self.negotiated in (Enc.AES, Enc.FULL_AES):
            self.crypter.set(self.negotiated, make_aes_key(self.nonce, self.cam.cfg.password))
        else:
            self.crypter.set(self.negotiated)
        log.info("%s: login OK, encryption=%s", self.tag, self.crypter.mode.name)

    def _get_reply(self, msg_id: int) -> bytes | None:
        """Reply body for a get message: stored setting > template."""
        tv = self.cam.template_vars(self.username)
        stored = self.cam.settings.get(msg_id)
        if stored is not None:
            return Template(stored.decode("utf-8")).safe_substitute(
                {k: str(v) for k, v in tv.items()}).encode("utf-8")
        return self.cam.templates.render(msg_id, tv)

    async def on_set(self, msg: BcMsg, get_id: int) -> None:
        try:
            merged = st.merge(self._get_reply(get_id), msg.payload)
        except Exception as e:  # noqa: BLE001 - bad XML: still ack, like a camera would
            log.warning("%s: set msg %d: cannot merge (%s)\n%s", self.tag, msg.msg_id, e, msg.payload_text)
            await self.send(msg.reply(200))
            return
        new_name = st.find_name(msg.payload)
        if new_name:
            self.cam.rename(new_name)
        self.cam.settings.put(get_id, st.templatize_name(merged, self.cam.display_name))
        log.info("%s: setting saved: msg %d -> %d%s", self.tag, msg.msg_id, get_id,
                 f" (name={new_name!r})" if new_name else "")
        await self.send(msg.reply(200))

    async def on_logout(self, msg: BcMsg) -> None:
        await self.send(msg.reply(200))
        self.writer.close()

    # ---------------------------------------------------------------- alarms
    async def on_alarm_start(self, msg: BcMsg) -> None:
        self.alarm_req = msg
        self.cam.alarm_subs.add(self)
        await self.send(msg.reply(200))
        log.info("%s: alarm subscription started", self.tag)
        self.push_alarm(self.cam.state.snapshot())

    # ---------------------------------------------------------------- video
    async def on_preview(self, msg: BcMsg) -> None:
        stype = xml_value(msg.payload, "streamType") or "mainStream"
        handle = int(xml_value(msg.payload, "handle") or 0)
        src = self.cam.main if stype == "mainStream" else self.cam.sub
        if src is None:
            await self.send(msg.reply(400))
            return
        if handle in self.previews:
            self.previews.pop(handle).cancel()
        self.previews[handle] = self._spawn(self._stream(msg, src, stype))

    async def on_preview_stop(self, msg: BcMsg) -> None:
        handle = int(xml_value(msg.payload, "handle") or 0)
        t = self.previews.pop(handle, None)
        if t:
            t.cancel()
        await self.send(msg.reply(200))

    async def _stream(self, req: BcMsg, src, stype: str) -> None:
        try:
            info = await src.ensure_info()
        except Exception as e:  # noqa: BLE001
            log.warning("%s: %s unavailable: %s", self.tag, stype, e)
            await self.send(req.reply(400))
            return
        q = src.subscribe()
        log.info("%s: preview %s started", self.tag, stype)
        frames = sent = 0
        t_start = time.monotonic()
        try:
            first = bcmedia.info_v2(info.width, info.height, info.fps)
            await self.send(req.reply(200, ext=ext_xml(binaryData=1), payload=first, binary=True))
            t0 = None
            waiting_key = True
            while True:
                item = await q.get()
                if item is None:          # backlog dropped
                    waiting_key = True
                    continue
                ts, au = item
                if waiting_key and not au.keyframe:
                    continue
                waiting_key = False
                if t0 is None:
                    t0 = ts
                micros = int((ts - t0) * 1_000_000)
                frame = (bcmedia.iframe(info.codec, au.data, micros, int(time.time()))
                         if au.keyframe else bcmedia.pframe(info.codec, au.data, micros))
                for i in range(0, len(frame), MEDIA_CHUNK):
                    await self.send(req.reply(200, payload=frame[i:i + MEDIA_CHUNK], binary=True))
                if frames == 0:
                    log.info("%s: preview %s first keyframe sent after %.1fs (%d bytes)", self.tag,
                             stype, time.monotonic() - t_start, len(frame))
                frames += 1
                sent += len(frame)
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError) as e:
            log.info("%s: preview %s ended: %s", self.tag, stype, e)
        finally:
            src.unsubscribe(q)
            log.info("%s: preview %s stopped after %.1fs: %d frames, %d KB", self.tag, stype,
                     time.monotonic() - t_start, frames, sent // 1024)

    # ------------------------------------------------------------- snapshot
    async def on_snap(self, msg: BcMsg) -> None:
        jpeg = await self.cam.snapshot_jpeg()
        if not jpeg:
            await self.send(msg.reply(400))
            return
        fname = time.strftime("01_%Y%m%d%H%M%S.jpg")
        await self.send(msg.reply(200, payload=body_xml(
            f'<Snap version="1.1">\n<channelId>0</channelId>\n<fileName>{fname}</fileName>\n'
            f"<time>0</time>\n<pictureSize>{len(jpeg)}</pictureSize>\n</Snap>")))
        chunks = [jpeg[i:i + SNAP_CHUNK] for i in range(0, len(jpeg), SNAP_CHUNK)]
        for i, c in enumerate(chunks):
            last = i == len(chunks) - 1
            await self.send(msg.reply(201 if last else 200, ext=ext_xml(binaryData=1),
                                      payload=c, binary=True))
