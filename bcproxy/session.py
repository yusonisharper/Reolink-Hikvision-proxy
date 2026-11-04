"""Server side of one Baichuan TCP connection (NVR -> virtual camera)."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time
from string import Template

from . import bc, bcmedia
from . import playback as pb
from . import hikcfg
from . import settings as st
from .bc import BcMsg, body_xml, ext_xml, xml_value
from .camera import VirtualCamera
from .crypto import Crypter, Enc, login_hash, make_aes_key
from .events import Snapshot, alarm_event_xml
from .media import read_access_units
from .templates import SMART_MSG, smart_detect_xml

log = logging.getLogger("bc")

MEDIA_CHUNK = 32 * 1024
# Real cameras answer these with an empty 200 too (192: seen in a neolink capture;
# 287: TimeCfg write from the app - Hikvision keeps its own NTP time).
QUIET_OK = {192, 287}
# What a real camera (dual-lens RLC, app capture) answers for features it lacks.
ERROR_REPLY = {257: 400, 789: 405, 818: 405}
# Read-only views of a setting that is written through another id: answer
# with the template, overlaid with what the client stored (132 = 26 + ranges).
READ_OVERLAY = {132: 26, 110: 44}
SNAP_CHUNK = 32 * 1024
MSG_ONLINE_USERS = 120
MSG_RECORD_CFG, MSG_RECORD_CFG_SET, MSG_COMPRESSION_SET, MSG_RECORD, MSG_RECORD_SET = 54, 55, 57, 81, 82
MSG_LIGHT_MANUAL, MSG_LIGHT_STATUS = 288, 291
MSG_IMAGE_SET, MSG_IMAGE, MSG_IMAGE_RANGES, MSG_PORTS, MSG_PORTS_SET = 25, 26, 132, 37, 38
MSG_TAMPER, MSG_TAMPER_SET = 763, 764
CONTROL_IDS = {MSG_RECORD_CFG, MSG_RECORD_CFG_SET, MSG_COMPRESSION_SET, MSG_RECORD, MSG_RECORD_SET, MSG_LIGHT_MANUAL,
               MSG_IMAGE_SET, MSG_IMAGE, MSG_IMAGE_RANGES, MSG_PORTS, MSG_PORTS_SET, MSG_TAMPER, MSG_TAMPER_SET}
PLAYBACK_IDS = {pb.MSG_HDD, pb.MSG_DAY_RECORDS, pb.MSG_FILE_OPEN, pb.MSG_FILE_LIST, pb.MSG_FILE_CLOSE,
                pb.MSG_COVER, pb.MSG_REPLAY, pb.MSG_REPLAY_STOP, pb.MSG_REPLAY_SEEK}


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
        self.searches: dict[int, tuple[dt.datetime, dt.datetime, str]] = {}   # record search handles
        self.next_handle = 0
        self.replay: asyncio.Task | None = None
        self.replay_file_no = 0
        self.replay_session: tuple[BcMsg, dt.datetime, str] | None = None   # (381 request, end, stream)
        self.tasks: set[asyncio.Task] = set()
        self.trace = trace
        self._wlock = asyncio.Lock()
        self.warned: set[int] = set()
        self.tag = f"{cam.cfg.name}<-{self.peer[0] if self.peer else '?'}"

    # ------------------------------------------------------------------- io
    async def send(self, msg: BcMsg) -> None:
        data = bc.encode(msg, self.crypter, self.decoder.magic or bc.MAGIC)
        if self.trace and not msg.binary:
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
            self.cam.sessions.discard(self)
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
            MSG_ONLINE_USERS: self.on_online_users,
        }.get(msg.msg_id)
        if handler:
            await handler(msg)
            return
        if self.cam.recordings and msg.msg_id in PLAYBACK_IDS:
            await self.on_playback(msg)
            return
        if self.cam.hikcfg and msg.msg_id in CONTROL_IDS:
            await self.on_control(msg)
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
        if msg.msg_id in ERROR_REPLY:
            await self.send(msg.reply(ERROR_REPLY[msg.msg_id]))
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
        self.cam.sessions.add(self)
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
        if stored is None and msg_id in READ_OVERLAY:
            over = self.cam.settings.get(READ_OVERLAY[msg_id])
            base = self.cam.templates.render(msg_id, tv)
            if over is not None and base is not None:
                try:
                    stored = st.merge(base, over)
                except Exception:  # noqa: BLE001 - fall back to the plain template
                    stored = None
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

    async def on_online_users(self, msg: BcMsg) -> None:
        users = "".join(
            f"<OnlineUser>\n<userId>0</userId>\n<sessionId>{i}</sessionId>\n"
            f"<userName>{s.username or self.cam.cfg.username}</userName>\n<userLevel>1</userLevel>\n"
            f"<ipAddress>{s.peer[0] if s.peer else ''}</ipAddress>\n<macAddress></macAddress>\n"
            f"<enableOutoffLine>0</enableOutoffLine>\n<isOnline>1</isOnline>\n</OnlineUser>\n"
            for i, s in enumerate(sorted(self.cam.sessions, key=id), 1))
        await self.send(msg.reply(200, payload=body_xml(
            f'<OnlineUserList version="1.1">\n{users}</OnlineUserList>')))

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
        if self.cam.hikcfg and self.cam.hikcfg.light_on_mode:
            self.push_light(self.cam.light_status)

    def push_light(self, on: bool) -> None:
        if self.writer.is_closing():
            return
        self._spawn(self.send(BcMsg(MSG_LIGHT_STATUS, response=200, payload=body_xml(
            '<FloodlightStatusList version="1.1">\n<FloodlightStatus>\n<channel>0</channel>\n'
            f"<status>{1 if on else 0}</status>\n</FloodlightStatus>\n</FloodlightStatusList>"))))

    # ------------------------------------------------- settings that reach Hikvision
    async def on_control(self, msg: BcMsg) -> None:
        mid, cfg = msg.msg_id, self.cam.hikcfg
        try:
            if mid == MSG_COMPRESSION_SET:
                await self.cam.apply_compression(msg.payload)
                await self.send(msg.reply(200))
            elif mid == MSG_LIGHT_MANUAL:
                await self.cam.set_light(xml_value(msg.payload, "status") == "1")
                await self.send(msg.reply(200))
            elif mid in (MSG_IMAGE, MSG_IMAGE_RANGES):
                template = self.cam.templates.render(MSG_IMAGE_RANGES, self.cam.template_vars(self.username))
                await self.send(msg.reply(200, payload=await cfg.image_xml(template, self.cam.frame_rate_mode)))
            elif mid == MSG_IMAGE_SET:
                changed = await cfg.apply_image(msg.payload)
                log.info("%s: image written to Hikvision: %s", self.tag, changed or "no change")
                rate = xml_value(msg.payload, "constantFrameRate")
                if rate is not None:
                    self.cam.set_frame_rate_mode(int(rate))
                await self.send(msg.reply(200))
            elif mid == MSG_TAMPER:
                on = await cfg.tamper_enabled()
                await self.send(msg.reply(200, payload=body_xml(
                    f'<tamperAlarmCfg version="1.1">\n<enable>{1 if on else 0}</enable>\n</tamperAlarmCfg>')))
            elif mid == MSG_TAMPER_SET:
                on = xml_value(msg.payload, "enable") == "1"
                await cfg.set_tamper(on)
                log.info("%s: Hikvision tamper detection %s", self.tag, "on" if on else "off")
                await self.send(msg.reply(200))
            elif mid == MSG_PORTS:
                await self.send(msg.reply(200, payload=body_xml(await cfg.server_ports_xml(self.cam.cfg.listen_port))))
            elif mid == MSG_PORTS_SET:
                changed = await cfg.set_server_ports(msg.payload, self.cam.cfg.listen_port)
                log.info("%s: server ports written to Hikvision: %s", self.tag, changed or "no change")
                await self.send(msg.reply(200))
            elif mid == MSG_RECORD:
                doc = self.cam.settings.get(MSG_RECORD) or body_xml(hikcfg.record_xml(
                    await cfg.track(), [t for t in ("people", "vehicle", "dog_cat") if t in self.cam.cfg.ai_types]))
                await self.send(msg.reply(200, payload=doc))
            elif mid == MSG_RECORD_SET:
                root = st._body(msg.payload).find("Record")
                tables = {i.findtext("type"): i.findtext("valueTable") or "" for i in root.iter("item")}
                await cfg.set_schedule(root.findtext("enable") != "0", tables)
                self.cam.settings.put(MSG_RECORD, msg.payload)
                log.info("%s: record schedule written to Hikvision", self.tag)
                await self.send(msg.reply(200))
            elif mid == MSG_RECORD_CFG:
                await self.send(msg.reply(200, payload=body_xml(await cfg.record_cfg_xml())))
            else:   # MSG_RECORD_CFG_SET
                num = lambda t: int(v) if (v := xml_value(msg.payload, t)) else None  # noqa: E731
                await cfg.set_record_cfg(num("cycle"), num("recordDelayTime"), num("preRecordTime"))
                await self.send(msg.reply(200))
        except Exception as e:  # noqa: BLE001 - Hikvision refused, bad request: tell the app
            log.warning("%s: msg %d failed: %s", self.tag, mid, e)
            await self.send(msg.reply(400))

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
        audio_task = None
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
                if src.info is not info and src.info:    # resolution changed from the settings page
                    info = src.info
                    await self.send(req.reply(200, payload=bcmedia.info_v2(info.width, info.height, info.fps),
                                              binary=True))
                if t0 is None:
                    t0 = ts
                    if self.cam.audio and self.cam.audio_on:
                        audio_task = self._spawn(self._audio(req))
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
            if audio_task:
                audio_task.cancel()
            src.unsubscribe(q)
            log.info("%s: preview %s stopped after %.1fs: %d frames, %d KB", self.tag, stype,
                     time.monotonic() - t_start, frames, sent // 1024)

    async def _audio(self, req: BcMsg) -> None:
        """Microphone frames, interleaved with the video of one preview request."""
        q = self.cam.audio.subscribe()
        try:
            while True:
                await self.send(req.reply(200, payload=bcmedia.aac(await q.get()), binary=True))
        except (ConnectionError, OSError):
            pass
        finally:
            self.cam.audio.unsubscribe(q)

    # ------------------------------------------------------------- playback
    async def on_playback(self, msg: BcMsg) -> None:
        rec, mid = self.cam.recordings, msg.msg_id
        try:
            if mid == pb.MSG_HDD:
                cap = await rec.storage()
                if cap is None:
                    raise ValueError("no disk")
                await self.send(msg.reply(200, payload=body_xml(pb.hdd_info_xml(*cap))))
            elif mid == pb.MSG_DAY_RECORDS:
                el = pb.body_root(msg.payload, "DayRecords")
                start, end = pb.parse_time(el.find("startTime")), pb.parse_time(el.find("endTime"))
                days = [dt.date(y, m, d) for y, m in pb.months_between(start.date(), end.date())
                        for d in sorted(await rec.record_days(y, m))
                        if start.date() <= dt.date(y, m, d) <= end.date()]
                await self.send(msg.reply(200, payload=body_xml(pb.day_records_xml(start, end, days))))
            elif mid == pb.MSG_FILE_OPEN:
                el = pb.body_root(msg.payload, "FileInfo")
                start, end = pb.parse_time(el.find("startTime")), pb.parse_time(el.find("endTime"))
                if not start or not end:
                    raise ValueError("no time range")
                self.next_handle += 1
                self.searches[self.next_handle] = (start, end, el.findtext("streamType") or "mainStream")
                await self.send(msg.reply(200, payload=body_xml(pb.file_open_xml(self.next_handle, start, end))))
            elif mid == pb.MSG_FILE_LIST:
                handle = int(xml_value(msg.payload, "handle") or -1)
                if handle not in self.searches:
                    raise ValueError(f"unknown handle {handle}")
                start, end, stream = self.searches[handle]
                found = await rec.search(start, end)
                xml, listed = pb.file_list_limited(
                    handle, stream, [pb.Segment(s.start, s.end, self.cam.journal.label(s)) for s in found], start, end)
                log.info("%s: record search %s .. %s: %d segment(s), listed as %d", self.tag, start, end,
                         len(found), listed)
                await self.send(msg.reply(200, payload=body_xml(xml)))
            elif mid == pb.MSG_FILE_CLOSE:
                self.searches.pop(int(xml_value(msg.payload, "handle") or -1), None)
                await self.send(msg.reply(200))
            elif mid == pb.MSG_REPLAY:
                self._stop_replay()
                el = pb.body_root(msg.payload, "ReplayByTimeV2")
                start, end = pb.parse_time(el.find("startTime")), pb.parse_time(el.find("endTime"))
                if not start or not end:
                    raise ValueError("no time range")
                stream = el.findtext("streamType") or "mainStream"
                self.replay_session = (msg, end, stream)
                self.replay = self._spawn(self._replay(msg, pb.seq_of(msg.payload), start, end, stream))
            elif mid == pb.MSG_REPLAY_SEEK:
                el = pb.body_root(msg.payload, "ReplaySeekV2")
                to = pb.parse_time(el.find("seekTime"))
                if not to or not self.replay_session:
                    raise ValueError("seek without a replay")
                self._stop_replay()
                req, end, stream = self.replay_session
                await self.send(msg.reply(200))
                log.info("%s: replay seek to %s", self.tag, to)
                self.replay = self._spawn(self._replay(req, pb.seq_of(msg.payload), to, end, stream, seek=True))
            elif mid == pb.MSG_REPLAY_STOP:
                self._stop_replay()
                self.replay_session = None
                await self.send(msg.reply(200))
            else:   # 298 timeline thumbnails
                await self.send(msg.reply(400))
        except Exception as e:  # noqa: BLE001 - Hikvision errors, bad requests
            log.warning("%s: playback msg %d failed: %s", self.tag, mid, e)
            await self.send(msg.reply(400))

    def _stop_replay(self) -> None:
        if self.replay and not self.replay.done():
            self.replay.cancel()
        self.replay = None

    async def _replay(self, req: BcMsg, seq: int, start: dt.datetime, end: dt.datetime, stream: str,
                      seek: bool = False) -> None:
        """Play the one recording at or after start, then stop: the user picks what plays next.
        req is the 381 that opened the session: its frames stay on that id, also after a seek."""
        rec = self.cam.recordings
        frames = sent = 0
        t0 = time.monotonic()
        try:
            try:
                info = await self.cam.main.ensure_info()
                found = await rec.search(start, end, limit=1)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: replay failed: %s", self.tag, e)
                found = []
            if not found:
                log.info("%s: no recording after %s", self.tag, start)
                if not seek:
                    await self.send(req.reply(400))
                return
            clip = found[0]
            begin = max(start, clip.start)
            if not seek:
                await self.send(req.reply(200))
            # after a seek this request id is in binary mode: say that this one is XML
            info_ext = ext_xml(binaryData=0, channelId=0) if seek else ext_xml(channelId=0)
            await self.send(req.reply(200, ext=info_ext, payload=body_xml(pb.replay_info_xml(
                seq, stream, info.width, info.height, info.fps, begin, clip.end))))
            log.info("%s: replay %s .. %s (seq %d)", self.tag, begin, clip.end, seq)
            self.replay_file_no += 1
            url = await rec.replay_url(self.cam.cfg.main_rtsp, begin, clip.end)
            length = (clip.end - begin).total_seconds()
            ext = ext_xml(binaryData=1, channelId=0)
            base_us = pb.wall_seconds(begin) * 1_000_000
            # a clip keeps the codec it was recorded with, which may not be today's
            for codec in dict.fromkeys((info.codec, "H265", "H264")):
                clip_t0 = None
                aus = read_access_units(url, codec)
                try:
                    async for au in aus:
                        now = time.monotonic()
                        if clip_t0 is None:
                            if not au.keyframe:
                                continue
                            clip_t0 = now
                        elapsed = now - clip_t0
                        if elapsed > length + 1:    # Hikvision does not end the stream by itself
                            break
                        micros = base_us + int(elapsed * 1_000_000)
                        wall = pb.wall_seconds(begin + dt.timedelta(seconds=elapsed))
                        frame = (bcmedia.iframe(codec, au.data, micros, wall, self.replay_file_no) if au.keyframe
                                 else bcmedia.pframe(codec, au.data, micros, self.replay_file_no))
                        for i in range(0, len(frame), MEDIA_CHUNK):
                            # replay frames carry the session seq where replies have response/class
                            await self.send(BcMsg(pb.MSG_REPLAY, req.channel, req.stream_type, req.msg_num,
                                                  seq & 0xFFFF, seq >> 16, ext, frame[i:i + MEDIA_CHUNK], True))
                        frames += 1
                        sent += len(frame)
                finally:
                    await aus.aclose()      # kills ffmpeg now, not at garbage collection
                if clip_t0 is not None:
                    break
        except asyncio.CancelledError:
            pass
        except (ConnectionError, OSError) as e:
            log.info("%s: replay ended: %s", self.tag, e)
        finally:
            log.info("%s: replay stopped after %.1fs: %d frames, %d KB", self.tag,
                     time.monotonic() - t0, frames, sent // 1024)

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
