# reolink-hik-proxy

**English** | [简体中文](README.zh-CN.md)

Makes Hikvision and Dahua cameras look like Reolink cameras, so a Reolink NVR can add them over the native Reolink protocol (Baichuan, TCP 9000). The main point is **event mapping**: people and vehicle detection, line crossing, intrusion and loitering become Reolink AI and smart detection events, instead of the plain motion that ONVIF can carry.

Unless stated otherwise, this document describes Hikvision. For Dahua, see [Dahua cameras](#dahua-cameras).

```
Hikvision IPC ──ISAPI alertStream / HTTP push──┐
              ──RTSP main/sub (ffmpeg -c copy)─┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
              ──ISAPI snapshot─────────────────┘
```

## Event mapping

| Hikvision eventType | Target (detectionTarget) | Reolink msg 33 output |
|---|---|---|
| `VMD` | none | `<status>MD</status>` |
| `VMD` (AcuSense) | human / vehicle | `MD` + `<AItype>people</AItype>` / `vehicle` |
| `linedetection` | human / vehicle | `<smartAiType><type>crossline</type>` + zone + target, along with AItype and MD |
| `fielddetection`, `regionEntrance` | human / vehicle | `intrusion` |
| `loitering` | human / vehicle | `loitering` |
| `shelteralarm`, `tamperdetection` | – | `<status>tamper</status>` |
| `scenechangedetection` (needs `event_map: {sceneChangeDetection: tamper}`) | – | `<status>tamper</status>`; the app's Tamper Alert switch controls both tamper and scene change |
| `faceCapture` (JSON, followed by JPEG) | face + people | Reported as both person and face, so the NVR's Person notification fires too; types not in `ai_types` are not reported |

- Hikvision's `regionID` (from 1) maps to the Reolink zone number `zone` (from 0).
- One-shot alerts such as line crossing are held for `hold.smart` seconds, so the NVR has time to start recording; VMD clears immediately on `inactive`.
- Whether the NVR considers a camera capable of these features depends on `<aitype>` (bit 1 = people, bit 2 = vehicle, bit 4 = dog/cat) and `<smartAI>` (bit 1 = line crossing, bit 2 = intrusion, bit 3 = loitering) in `<Support>` of msg 199. Both values are generated from `ai_types` and `smart` in the config, and so are the zone lists of msg 527/529/531.

## Dahua cameras

Replace `hik:` with `dahua:` in the config. See `config.example.yaml` for an example.

```
Dahua IPC ──eventManager.cgi event stream──────┐
          ──RTSP main/sub (ffmpeg -c copy)─────┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
          ──snapshot / configManager / mediaFileFind┘
```

| Dahua event Code | Target | Reolink msg 33 output |
|---|---|---|
| `VideoMotion` | none | `MD` |
| `SmartMotionHuman` / `SmartMotionVehicle` | from the Code | `MD` + `people` / `vehicle` |
| `CrossLineDetection` | `Object.ObjectType` | `crossline` |
| `CrossRegionDetection` | `Object.ObjectType` | `intrusion` |
| `WanderDetection` | `Object.ObjectType` | `loitering` |
| `VideoBlind` | – | `tamper` |

Dahua sends `Start` and `Stop` once each. The proxy repeats the event every second in between, which works the same as Hikvision's continuous alerts, and treats the `Stop` as lost after 10 minutes. Except for plain motion, `Stop` does not clear the state right away: it waits for the `hold` time to end.

**Status:** Reading, preview and PTZ were verified with `tools/bc_client.py` on one real camera (Amcrest `IP4M-1083E-AI`, a rebranded Dahua SD5A432XB-HNR, firmware 2.810, 2560×1440 H.265). It has not been connected to a Reolink NVR or app, and writing settings and people, vehicle and smart events have not been triggered on a real camera. The rest was only tested with `tools/fake_dahua.py`, and other models and firmware may differ. When connecting a new model for the first time, run with `-v`, and check that the `RuleId` of events matches `region_base` and that `encode.cgi getConfigCaps` yields resolutions (if it cannot be parsed, only the current resolution is offered).

| Feature | Dahua |
|---|---|
| Main/sub stream preview, snapshot | ✅ Verified on a real camera (`bc_client`, no NVR or app) |
| Audio, AI and smart events | 🟡 Connected, to be verified on a real camera (the event stream connects, only `NewFile` seen so far) |
| SD card playback | 🟡 Recording search and capacity read on a real camera; RTSP `/cam/playback` to be verified |
| Streams (resolution, frame rate, bitrate, CBR/VBR, audio switch) | 🟡 Capabilities and current values read on a real camera; writing `Encode` to be verified |
| Image page | 🟡 Only brightness, contrast, saturation, sharpness, mirror, flip and day/night; the rest has no effect |
| Tamper Alert switch, renaming | 🟡 Writes `BlindDetect` and `ChannelTitle`, to be verified on a real camera |
| PTZ: direction, zoom, go to preset | ✅ Tested in the app (the zoom slider not confirmed separately) |
| PTZ: save/delete presets, patrols | 🟡 Verified on the Dahua side on a real camera, the app to be verified (see below) |
| PTZ: manual focus | ❌ Not connected |
| Recording schedule and settings (54/55/81/82) | ❌ Not connected and not declared to the app; set them on the Dahua web page |
| Server ports (37/38), manual light, CPU usage, HTTP push events | ❌ Not connected |

Note: Dahua may lock the account after repeated failed logins, and with a wrong password the proxy's reconnects can trigger that.

### PTZ

| Baichuan message | Dahua |
|---|---|
| 18 `PtzControl` (left/right/up/down/diagonals/stop, speed 1–64) | `ptz.cgi` start/stop, speed scaled to 1–8 |
| 18 zoom (zoomIn/zoomInc, zoomOut/zoomDec) | `ZoomTele` / `ZoomWide` |
| 190 / 19 `toPos`, `setPos`, `delPos` | `getPresets` / `GotoPreset`, `SetPreset` + `PtzPreset[].Name`, `ClearPreset` (only presets 1–128 are shown and changed) |
| 18 `startPatrol` / `stopPatrol` + `patrolId` | `StartTour` / `StopTour` |
| `<PtzCruise>` (writes are recognized by tag, for the read message ids see below) | `PtzTour` config table |
| 294 / 295 `zoomPos` | `ZoomMapValue` (0–16384) / see below |

- **Sources:** `ptzType=2` and `ptzPreset` come from reolink_aio; command names and the fields of `PtzCruise` and `PtzPreset` come from `libBCSDKWrapper.dll` of Reolink Client. `ptzMode=pt`, `ptzControl` and `ptzCfg` have no capture from a real camera. When the app sends an unknown command, the log prints `PTZ msg ... failed` with the full XML: add it to `dahua.PTZ_CODES`.
- **Safety:** Movement stops at once when the client disconnects, and also when no stop arrives within `ptz_max_seconds` (default 30 s). A failed stop is retried 3 times. A proxy that is killed cannot send a stop.
- **Presets:** The app only sees presets 1–128, and saving and deleting only accept that range. Later presets stay untouched on the camera. Saving to an existing number overwrites it.
- **Patrols:** These map to Dahua Tours. Only routes with presets are listed, up to `caps.TourMax`. On this camera the minimum dwell time is 15 s, and smaller values are raised to 15.
- **Two numbering schemes in patrols (confirmed on a real camera):** The app's `patrolId` starts at 0, Dahua's Tour at 1. Presets in the `PtzTour` table are zero-based (preset 7 is stored as 6, `-1` means empty). Getting this wrong raises no error: the camera just goes to the wrong place.
- **Reading patrols:** When the app opens its PTZ panel, it sends 294, 190 and 64 in turn. If 64 returns nothing, the panel says "failed to load", so patrol reads are answered with `<PtzCruise>` (`session.MSG_CRUISE_READS`).
- **Absolute zoom:** `PositionABS` on this camera cannot set the zoom: every call zooms in one step, whatever `arg3` is. So 295 uses timed pulses with correction from reading the position back (`ZOOM_RATE`). The measured error is about 70 from wide to mid range and about 600 at the tele end (full scale 16384). The speed parameter has almost no effect on zoom.
- `MoveStatus` of `getStatus` reports Idle too early when going to a preset. To tell whether it arrived, check that the position has stopped changing.
- The camera's own return-home task (PtzAutoMovement) is outside the proxy's control and may turn the view back after an app operation.

## Quick start

```bash
pip install -r requirements.txt        # cryptography + PyYAML; ffmpeg/ffprobe are also required
cp config.example.yaml config.yaml     # fill in Hikvision address/account/RTSP, the virtual camera's IP and password
python -m bcproxy -c config.yaml -v
```

Then add the camera manually in the Reolink NVR: use the IP from `listen`, port 9000, and the `username` and `password` from the config.

**One IP per Hikvision camera:** The NVR tells cameras apart by IP. On Linux, add alias IPs to the proxy host (`ip addr add 192.168.1.201/24 dev eth0`) or use Docker macvlan. For Windows, see below.

## Local testing without an NVR

```bash
# Terminal 1: fake Hikvision (Digest auth + alertStream + snapshot); type events: vmd human / line 1 vehicle / field 2 human
python tools/fake_hik.py --port 8080
# Terminal 2: the proxy (hik.host = 127.0.0.1:8080; main_rtsp can be a local mp4 file)
python -m bcproxy -c config.yaml -v
# Terminal 3: Baichuan client, subscribes to alerts and prints msg 33
python tools/bc_client.py 127.0.0.1 -p change-me --get 199 527 --alarms --seconds 60
python tools/bc_client.py 127.0.0.1 -p change-me --preview subStream --out sub.h264 --snap a.jpg
```

For Dahua, use `python tools/fake_dahua.py --port 8081` (account `admin` / `dahua12345`). It accepts `motion`, `human`, `human off`, `line 1 Human`, `region 2 Vehicle` and `blind`.

## Compatibility status

Tested on a real Reolink NVR and the Reolink mobile app (Hikvision 2688×1520 H.265 main stream + 640×360 sub stream):

| Feature | Status | Notes |
|---|---|---|
| Manual add / login | ✅ Verified | FULL_AES (0x12) encryption, echoes the NVR's header magic `0x0FEDCBA0` |
| Live preview (main/sub) | ✅ Verified | Works on both NVR and app |
| Renaming a camera | ✅ Verified | Saved to `state_dir` and synced to the Hikvision channel name (`hik.sync_name`) |
| AI and smart detection events | 🟡 Code done | msg 31/33; recording triggers and icons still to be confirmed on the NVR |
| Stream settings page (56 / 146) | 🟡 To verify | v7 added `videoEncType`; bitrate now reads Hikvision's actual config |
| Bitrate / CPU usage | 🟡 To confirm on the NVR | 122 answers `<PerformanceInfo>` (`cpuUseRate`, `codeRate`, `netDataRate`); message id and fields come from Reolink Client's SDK; Dahua has no CPU value and shows 0 |
| App settings pages | 🟡 To verify | 37 ports, 42 email, 46 motion detection, 52 privacy mask, 76 network, 112 streams, 120 online users, 132 image; formats follow real captures |
| Stream resolution | 🟡 To verify in the app | 146 lists all main stream resolutions Hikvision offers (the sub stream is fixed, as on real cameras); writing 57 changes Hikvision's resolution, frame rate and bitrate and reconnects the video. The app's `videoEncType` and `encoderType` are unreliable and ignored |
| Recording settings page | 🟡 To verify in the app | 81/82 ↔ Hikvision's recording schedule (Normal = CMR, other types = EDR), 54/55 ↔ overwrite, pre-record, post-record |
| Advanced network settings page | 🟡 To verify in the app | 40/97/210/455, only stored in the proxy, Hikvision's network is not changed |
| Tamper Alert | 🟡 To verify in the app | Bit 25 of `aitype` in 199 declares it, 763/764 read and write Hikvision's tamper detection switch; Hikvision's `shelteralarm` is reported as `tamper` |
| Manual light | 🟡 To verify in the app | 288 → Hikvision supplementLight, back to the previous mode after `light_seconds` (default 30 s), 291 pushes the state |
| Audio | 🟡 To verify in the app | Main stream audio is transcoded to 16 kHz AAC and sent with the preview (preview only; Hikvision has no speaker here, so no two-way audio) |
| Motion Boxes | ❌ Not implemented | No related messages in the existing captures; needs a capture from a real camera with this feature on |
| Playback | 🟡 To verify | Hikvision SD card: ISAPI recording search + RTSP playback (only the selected clip, stops at its end) → 102/142/14/15/16/381/382, seeking 383; off automatically when Hikvision has no storage. 298 timeline thumbnails are not implemented |
| LAN discovery | 🟡 Code done | UDP 2000, reply format follows real captures; the NVR's search packets are confirmed received and answered, display in the NVR's "add camera" list still to be confirmed |
| Other messages | ❌ Unknown | 110, 113, 122 etc. answer an empty 200; 257 answers 400, 789/818 answer 405 (same as a real camera) |

### Performance

Measured (2.0 Mbps main stream): parsing, packaging and encryption about 0.16% of one core, ffmpeg forwarding about 2%, audio transcoding about 1.6%. CPU is not the bottleneck. The wait for the first frame (1.3–2 s) is mostly RTSP and ffmpeg's analysis. `-fflags nobuffer` and a small `-probesize` give no picture on the main stream and in playback, so don't use them.

### ffmpeg hangs

- **stderr must be read all the time:** Otherwise its buffers fill up and ffmpeg blocks writing to stderr. It stops reading the camera, the camera then disconnects, and the process stays alive. The Crossroad sub stream keeps reporting `non monotonically increasing dts`, and with concat input it hung after about 8.5 minutes. Direct input reports fewer of these and hangs more slowly.
- **ffmpeg does not exit when a connection goes silent,** only when it closes. So a stream with no data for `STALL_SECONDS` (20 s) gets a new ffmpeg and a `no data` line in `errors.log`. If that line shows up often, there is a problem with the camera or the network.

### TODO: seeking in playback

Dragging the progress bar still has problems (the exact behavior in the app is yet to be written down). The reply format of 383 is inferred, with no sample from a real camera, and a capture of seeking on a real camera is still missing. To stay under the size limit of one message, the recording list merges segments, and seeking into a gap this creates lands on the next real recording.

### TODO: Motion Boxes

The box data in the extension area of video frames (captured from an NVR main stream, drawn locally by the app, with no switch field in the preview request): `ff <len16> { 01 0b00 { 01 0800 <u64 time> } 02 <len16> { 01 0400 "lz4 " 02 <len16> <LZ4 frame> } }`. I frames have another 8 bytes of time in front. Decompressed, it is nested TLV (`<u8 type> <u16 length> <value>`), on a 640×352 grid (type 3). Each box is type 4: `x1 y1 x2 y2 score` (u16), optionally with a tracking ID and class.

Real-time object coordinates are still missing: Hikvision's RTSP only has video and audio. Playback in Hikvision's app can show boxes, so the data should be stored with the recordings, and that is where to look next. The app's switch may be controlled by `thumbnail`, `ptzControl` or bits 6 and 7 of `newIspCfg` in 199, not verified.

### Protocol pitfalls

- **Concurrency on one connection:** The read loop does not wait for requests to finish. Otherwise the socket isn't even read during a slow request, and a PTZ `stop` never gets handled. Login is the exception: decrypting the next message depends on it.
- **Playback filter types:** `<recordType>` of 14 must list `crossline,intrusion,loitering`. A clip's `recordType` keeps `md`, and only `sched` is dropped when other types are present. Bit 0 of `aitype` in 199 is Motion: in Reolink Client's SDK, `GetSupportMDEvent` reads this bit and `GetSupportPeopleEvent` reads bit 1 of the same field.
- **Event types in notifications:** Tamper is bit 13 of `aitype` (bit 25 only stands for the settings switch). Line crossing, intrusion and loitering are bits 1–3 of `smartAI`.
- **Cameras without target detection:** When `aitype` declares no people or vehicle but line crossing and intrusion are declared, the layout of the NVR's notification settings page breaks (confirmed on a real DS-2CD2347G1-L). So these cameras also get `ai_types: [people, vehicle]` and `targets: [people, vehicle]`. The Person and Vehicle options on the page never fire. Leave `untargeted_smart_target` unset, so line crossing and intrusion are not reported as a person.
- **Don't declare `eventTypeVersion`:** With this field in 199, the playback filter lists events the camera doesn't have, such as Answered Call and Crying Sound (these names are built into the Reolink client). Without it those events are gone, and line crossing and intrusion stay in the NVR's notification settings (confirmed on a real camera).
- **Light and IR buttons:** The white light comes from `ledCtrl` and `lightType` in 199. `ledState` in 151 is the LED/IR control. Declaring it adds an IR button, so it isn't declared. Hikvision's white light is usually at `supplementLight`. DS-2CD2347G1-L doesn't have that endpoint (HTTP 403): its switch is `SupplementLight` in `/ISAPI/System/externalDevice`. Its `auto` mode stays off in daytime, so turning the light on sets an all-day `schedule` and manual brightness, and turning it off writes the previous config back.
- **Alert push:** In the logs, a real NVR and the mobile app never sent 31 (subscribe) after logging in. So 33 is pushed to every logged-in connection without waiting for a subscription.
- **Switching files in playback:** A real camera sends `replayStreamInfo` only once. When the next file starts, the last field of the frame header (the file number) changes, `micros` gets a new base, and the I frame carries that file's real time, with no other message (Duo 2 capture). The proxy doesn't switch automatically for now; this is only for the record. To do so, one Hikvision RTSP stream can cross recording gaps seamlessly, with `-use_wallclock_as_timestamps 1` to avoid timestamp errors.
- **End of a playback clip:** Hikvision's RTSP playback neither sends frames nor disconnects after `endtime`, so the end is detected by timeout.
- **Size of one message:** The app hangs on messages over about 40 KB (the largest on a real camera is 39488 bytes), so the recording list must stay within this size.
- **Bitrate mode:** The app's Bitrate Mode doesn't go through 57. It is `encType` in image message 25 (real capture), matching CBR/VBR of Hikvision's two streams.
- **Capability bits:** The app decides which options to show from the fields of 199, and a missing field doesn't mean unsupported. `binoCfg` must be an explicit 0, or Sync Brightness appears, which only the Duo series has. The bits of `newIspCfg`: 0 day/night, 1 anti-flicker, 2 exposure, 8 brightness, 9 contrast, 10 saturation, 11 hue, 12 sharpness (from reolink_aio). The value here is 5891, declaring only what is connected to Hikvision.
- **Header magic:** A Reolink NVR uses `0x0FEDCBA0` (`a0 cb ed 0f`), the app and neolink use `0x0ABCDEF0`. A camera must answer with the magic its peer sent. Otherwise the NVR discards the reply and disconnects after about 200 ms, which its UI shows as "connection timeout".
- **Encryption:** An NVR requests FULL_AES (`0xdc12`) and disconnects at once on a downgrade to AES (0x02). Binary payloads (video, JPEG) are encrypted whole, with `<encryptLen>` in the extension header. Real cameras also send `checkPos/checkValue` and encrypt only the first 1024 bytes. We don't, and the NVR and app decode fine.
- **LAN discovery:** Every 2 seconds, an NVR broadcasts the 4 bytes `aa aa 00 00` from UDP 3000 to `255.255.255.255:2000`, and Reolink Client searches the same way. A device answers with a fixed 388-byte structure (IP, port, name, MAC, UID; for field offsets see `bcproxy/discovery.py`). The reply **always goes to the peer's UDP 3000**, whatever the source port of the request. The proxy listens on `0.0.0.0:2000` and answers each search with one packet per virtual camera. `tools/bc_discover.py` lists real and virtual cameras on the LAN.
- **Settings messages:** Settings written by the NVR or app (25/45/57/105/209/297, and pairs of "read N, write N+1") are merged into the matching read replies and saved to `state_dir/<camera>/<id>.xml`. Delete the file to restore the default.

## Running on Windows

- The IP in `listen` must already exist on a local network adapter. To add an alias IP, first switch the adapter to a static IP, then run
  `netsh interface ipv4 add address "Ethernet" 192.168.0.201 255.255.255.0`.
- Port 9000 may be reserved by Hyper-V, WSL or Docker (binding fails with `could not bind`). Check with `netsh interface ipv4 show excludedportrange protocol=tcp`. If it is reserved, run `net stop winnat`, then `netsh int ipv4 add excludedportrange protocol=tcp startport=9000 numberofports=1`, and finally `net start winnat`.
- Allow inbound TCP 9000 and UDP 2000 (LAN discovery) through the firewall:
  `New-NetFirewallRule -DisplayName "bcproxy 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow`
  `New-NetFirewallRule -DisplayName "bcproxy search" -Direction Inbound -Protocol UDP -LocalPort 2000 -Action Allow`
- While Reolink Client runs on the same machine, it holds UDP 3000, and `tools/bc_discover.py` cannot run (the proxy itself is not affected).
- To save the full log: `python -m bcproxy -c config.yaml -v *> log.txt`

## Deploying to a UniFi Cloud Key Gen2 Plus

Verified on a UCK-G2-Plus (UniFi OS 6.0.11, Debian 13, aarch64). All files live in `/data/bcproxy`, and systemd starts the service at boot. First install (Git Bash, with `UCK-G2-Plus` set up in `~/.ssh/config`):

```bash
deploy/push.sh --with-config      # copies code, config.yaml and state/, downloads Python and ffmpeg, starts the service
deploy/push.sh                    # later code updates: leaves the device's config and state/ alone
```

**One IP per camera:** An NVR does not accept different ports on one IP. Put `alias_interface: eth0` in `config.yaml` and give each camera `listen: 192.168.0.5x:9000`. The proxy adds these IPs to the interface at startup, checks them every 30 seconds while running, and adds back any that are lost, with a line in `errors.log`. `/etc/systemd/network/eth0.network` is not changed, because UniFi OS generates that file. Keep these IPs out of the router's DHCP pool. When adding a camera to the NVR, use its IP and port 9000.

**Logs** (`/data/bcproxy/logs/`, rotated, at most 6 files of 5 MB each):

| File | Content |
|---|---|
| `errors.log` | WARNING and up; look here first |
| `bcproxy.log` | Everything, at the level set by `log_level` |
| `crash.log` | Python stacks from a fatal signal, including where a process killed by the watchdog was stuck |
| `restarts.log` | The reason for each exit: `result=watchdog`, `oom-kill`, `exit-code` and so on |

Passwords in RTSP URLs and XML are replaced with `***` before writing.

**Diagnostics upload:** UniFi OS uploads the reports in `/persistent/report/` to Ubiquiti every hour, and the report from any process crashing on the device includes the full `ps` output. So ffmpeg's RTSP URL (with its password) is not on the command line, but in an ffconcat file in a memfd. `LimitCORE=1` keeps the proxy's own crashes from creating reports (0 won't do: the kernel ignores 0 for a piped `core_pattern`).

Useful commands:

```bash
systemctl status bcproxy
tail -f /data/bcproxy/logs/errors.log
journalctl -u bcproxy -b
```

**Watchdog:** `Type=notify` + `WatchdogSec=60`. The heartbeat is sent from the event loop, so the service restarts when the loop hangs for 60 seconds or when a camera's background task (such as its event stream) dies. Whatever the reason for an exit, the service restarts after 10 seconds, with no limit on restarts.

**Firmware updates:** An update runs `overlay_cleanup` (`/usr/share/initramfs-tools/scripts/ubnt-init/apq8053/boot-emmc`), which removes `/usr`, `/lib`, `/var` (except `var/log`) and parts of `/etc`, and keeps `/data` and the service files in `/etc/systemd/system`. That is why this doesn't use apt or the firmware's Python. `ubnt-dpkg-restore` only restores packages on the official whitelist, so packaging this as a deb would not help.

This list is part of the firmware's script and may change. After an update, check `systemctl is-enabled bcproxy`. If the service is gone, restore it with the following command, which needs no network:

```bash
sh /data/bcproxy/app/deploy/install.sh
```

**A factory reset wipes `/data`**, and then the only way back is to run `deploy/push.sh --with-config` again. Keep a copy of `config.yaml` on the PC, and back up the device's `state/` if you need it.

## Filling in missing messages from a real camera

1. Export replies from a real Reolink camera:
   `python tools/bc_client.py <camera IP> -p <password> --dump-templates captured/ --get 26 46 56 76 110 112 113 122 132 146 208`
   Add `--nvr-magic` to imitate the NVR's header.
2. Or capture between the NVR and a real camera, then decrypt:
   `python tools/bc_dump.py nvr.pcap -p <camera password> --templates captured/`
   When capturing from a mobile app is impractical, use the relay: `python tools/bc_relay.py <camera IP> --listen 0.0.0.0:9001 --out app.pcap`, add "this PC's IP:9001" by IP in the app, then decrypt `app.pcap` with bc_dump.py.
3. `captured/<id>.xml` takes priority over the built-in templates. Fields such as model and UID can become placeholders like `${model}` and `${uid}`.
4. With `trace: true`, `unhandled msg N` in the log marks messages that still lack a template. Each ID is warned about once per connection.

## Project layout

```
bcproxy/
  crypto.py     BCEncrypt XOR / AES-128-CFB, login hash and key derivation
  bc.py         Baichuan frame codec (header, magic, extension XML, binary mode, FULL_AES)
  bcmedia.py    BcMedia container (InfoV2 / I frame / P frame / AAC), Annex-B access unit splitting
  session.py    Server session: login negotiation, template replies, settings writes, alert subscription (31/33), preview (3/4), snapshot (109)
  camera.py     Virtual camera: Hikvision events → state machine → push; capability bits, renaming, Hikvision status polling
  discovery.py  LAN discovery replies (UDP 2000 → 3000)
  hikcfg.py     Settings written to Hikvision: streams, recording schedule, light
  dahua.py      Dahua: event stream, config tables, SD card recordings (same interface as hik/hikcfg/HikRecordings)
  playback.py   Playback: recording search, event tags, replay stream
  settings.py   Merging and persisting settings written by the NVR or app
  events.py     Event state machine + <AlarmEventList> generation
  hik.py        ISAPI: Digest auth, alertStream, multipart parsing, alert mapping, HTTP push receiver
  media.py      ffprobe/ffmpeg pulling RTSP, one ffmpeg shared by all viewers, bitrate statistics
  logsetup.py   Console and rotating log files, passwords removed before writing
  watchdog.py   systemd readiness notification and watchdog heartbeat, checks that background tasks are alive
  netalias.py   Adds the cameras' listen IPs to the network interface and keeps them there
  templates/    Built-in reply templates (can be overridden by templates_dir)
deploy/         Cloud Key deployment: systemd service, install script for the device, push script for the PC
tools/
  bc_client.py  Baichuan client (tests the proxy / exports templates from a real camera)
  bc_dump.py    pcap/pcapng decryption tool
  bc_discover.py LAN search (lists real and virtual cameras)
  bc_relay.py   Recording relay between app/NVR and a real camera (writes a pcap for bc_dump.py)
  fake_hik.py   Fake Hikvision ISAPI service
  fake_dahua.py Fake Dahua CGI service (with PTZ)
tests/          Protocol tests (using real camera samples from neolink) + end-to-end tests: python -m pytest
```

## Other limitations

- Not implemented: two-way audio, Hikvision PTZ, Motion Boxes, UDP/P2P connections. Playback is video only (no audio), at a fixed 1× speed.
- The image page (25/26/132) reads and writes Hikvision: brightness, contrast, saturation, sharpness, mirror and flip, power frequency, day/night mode, white balance, 3D noise reduction. Exposure, gain, backlight and hue are not connected, and saving them has no effect.
- Server settings (37/38) read and write Hikvision: HTTPS and ONVIF can be changed. Disabling Hikvision's HTTP or RTSP or changing their ports is refused, because the proxy depends on them.
- Settings only stored in the proxy and never written to Hikvision: motion detection (47), OSD, network pages (DHCP, DDNS, UPnP).
- People, vehicle and smart types in playback come from alerts received while the proxy was running (`state/<camera>/events.jsonl`). Hikvision's recording index doesn't distinguish them, so older recordings show only motion.
- Timestamps are the time the proxy received a frame, not the camera's PTS.
- The geometry of smart detection zones (line and polygon coordinates) is not emulated. If the NVR needs to show or edit zones, use 527/529/531 templates captured from a real camera.
- 26 (image parameters) and 208 (LED/IR light) use default templates written with reference to reolink_aio, with fixed values.

## Credits

Protocol details draw on [neolink](https://github.com/QuantumEntangledAndy/neolink) and [reolink_aio](https://github.com/starkillerOG/reolink_aio). The real camera samples in `tests/samples/` come from neolink.
