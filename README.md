# reolink-hik-proxy

**English** | [简体中文](README.zh-CN.md)

Makes Hikvision and Dahua cameras look like Reolink cameras, so a Reolink NVR can add them over the native Reolink protocol (Baichuan, TCP 9000). The main point is **event mapping**: people and vehicle detection, line crossing, intrusion and loitering become Reolink AI and smart detection events, instead of the plain motion that ONVIF can carry.

Unless stated otherwise, this document describes Hikvision. For Dahua, see [Dahua cameras](#dahua-cameras).

```
Hikvision IPC ──ISAPI alertStream / HTTP push──┐
              ──RTSP main/sub (ffmpeg -c copy)─┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
              ──ISAPI snapshot─────────────────┘
```

Contents: [Getting started](#getting-started) · [Event mapping](#event-mapping) · [Compatibility status](#compatibility-status) · [Hikvision H.265 and Reolink downloads](#hikvision-h265-and-reolink-downloads) · [Motion Boxes](#motion-boxes) · [Dahua cameras](#dahua-cameras) · [Development](#development) · [Technical notes](#technical-notes)

## Getting started

### Quick start

```bash
pip install -r requirements.txt        # cryptography + PyYAML; ffmpeg/ffprobe are also required
cp config.example.yaml config.yaml     # fill in Hikvision address/account/RTSP, the virtual camera's IP and password
python -m bcproxy -c config.yaml -v
```

Then add the camera manually in the Reolink NVR: use the IP from `listen`, port 9000, and the `username` and `password` from the config.

**One IP per Hikvision camera:** The NVR tells cameras apart by IP. On Linux, add alias IPs to the proxy host (`ip addr add 192.168.1.201/24 dev eth0`) or use Docker macvlan. For Windows, see below.

### Running on Windows

- The IP in `listen` must already exist on a local network adapter. To add an alias IP, first switch the adapter to a static IP, then run
  `netsh interface ipv4 add address "Ethernet" 192.168.0.201 255.255.255.0`.
- Port 9000 may be reserved by Hyper-V, WSL or Docker (binding fails with `could not bind`). Check with `netsh interface ipv4 show excludedportrange protocol=tcp`. If it is reserved, run `net stop winnat`, then `netsh int ipv4 add excludedportrange protocol=tcp startport=9000 numberofports=1`, and finally `net start winnat`.
- Allow inbound TCP 9000 and UDP 2000 (LAN discovery) through the firewall:
  `New-NetFirewallRule -DisplayName "bcproxy 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow`
  `New-NetFirewallRule -DisplayName "bcproxy search" -Direction Inbound -Protocol UDP -LocalPort 2000 -Action Allow`
- While Reolink Client runs on the same machine, it holds UDP 3000, and `tools/bc_discover.py` cannot run (the proxy itself is not affected).
- To save the full log: `python -m bcproxy -c config.yaml -v *> log.txt`

### Deploying to a UniFi Cloud Key Gen2 Plus

Verified on a UCK-G2-Plus (UniFi OS 6.0.11, Debian 13, aarch64). All files live in `/data/bcproxy`, and systemd starts the service at boot. First install (Git Bash, with `UCK-G2-Plus` set up in `~/.ssh/config`):

```bash
bcproxy/native/build.sh aarch64   # in WSL or Linux: the H.265 tile merging library for the Cloud Key's CPU
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

## Compatibility status

Tested on a real Reolink NVR and the Reolink mobile app (Hikvision 2688×1520 H.265 main stream + 640×360 sub stream):

| Feature | Status | Notes |
|---|---|---|
| Manual add / login | ✅ Verified | FULL_AES (0x12) encryption, echoes the NVR's header magic `0x0FEDCBA0` |
| Live preview (main/sub) | ✅ Verified | Works on both NVR and app |
| Downloads to the phone and Reolink Client | ✅ Verified | Hikvision H.265, tiled cameras included, clear and fluent. See [Hikvision H.265 and Reolink downloads](#hikvision-h265-and-reolink-downloads) |
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
| Motion Boxes | 🟡 To verify in the app | Live preview only, with the camera's own target boxes, on the cameras that send them. Checked end to end up to Reolink Client's own parser; drawing on screen and passing through the NVR are not yet seen. See below |
| Playback | 🟡 To verify | Hikvision SD card: ISAPI recording search + RTSP playback (only the selected clip, stops at its end) → 102/142/14/15/16/381/382, seeking 383; off automatically when Hikvision has no storage. 298 timeline thumbnails are not implemented |
| LAN discovery | 🟡 Code done | UDP 2000, reply format follows real captures; the NVR's search packets are confirmed received and answered, display in the NVR's "add camera" list still to be confirmed |
| Other messages | ❌ Unknown | 110, 113, 122 etc. answer an empty 200; 257 answers 400, 789/818 answer 405 (same as a real camera) |

### Performance

Measured (2.0 Mbps main stream): parsing, packaging and encryption about 0.16% of one core, ffmpeg forwarding about 2%, audio transcoding about 1.6%. CPU is not the bottleneck. The wait for the first frame (1.3–2 s) is mostly RTSP and ffmpeg's analysis. `-fflags nobuffer` and a small `-probesize` give no picture on the main stream and in playback, so don't use them.

On the Cloud Key Gen2 Plus (8 × Cortex-A53, 2.0 GHz) with 8 cameras, an NVR watching all main and sub streams, Motion Boxes on 6 cameras and one microphone: Python takes about 47% of one core, the 17 ffmpeg processes about 29% together, and the device as a whole stays over 80% idle. The proxy is a single asyncio process, so one core is its limit: still twice the present load. Profiled with py-spy, the main costs are AES for the video (FULL_AES encrypts all of it, see Technical notes), the event loop waking for each TCP segment of the ffmpeg pipes and the Motion Boxes streams (data comes in real time, one segment at a time), and the H.265/H.264 parsing. The parser scans each byte once and never copies a NAL unit to read its header; the Motion Boxes reader parses whole reads, not one WebSocket frame at a time; one watchdog per ffmpeg replaces a timeout per read. Together these took the proxy from 53% to 47% of one core.

## Hikvision H.265 and Reolink downloads

Some Hikvision models code each picture as several tiles, each in its own slice: DS-2CD2347G1-L and DS-2CD2347G2-LU have 3 on the main stream, and the G1's sub stream has 2. Reolink's own cameras send one slice per picture, and some of Reolink's code does not cope with more. The proxy works around this without re-encoding: ffmpeg decodes the rewritten streams to the same pictures (checked with `framemd5`).

| Problem | Effect | What the proxy does |
|---|---|---|
| VPS/SPS declare 2 temporal sub-layers (G1, even with SVC off) | The NVR's decoder switches to its SVC reference handling and fails on the P frames | Rewrites the VPS/SPS to one sub-layer (`hevcps.single_sub_layer`), only when no reference picture set keeps more than one short-term picture |
| A VPS/SPS/PPS longer than 64 bytes, start code included (G1 SPS: 64 bytes, 81 with H.265+) | Reolink's mp4 writer gives up: the phone app reports a download error, NVR backups are empty | Drops the VUI HRD parameters from the SPS (`hevcps.fit_mp4_limit`), which gives 47 bytes. Decoders don't use them, and the camera sends no SEI that refers to them |
| A GOP is written with one `writev()` of at most 600 pieces, two per NAL unit, so pictures per GOP × slices must stay at or below 300 | The writer drops the whole GOP (`v_io_count to big:600`), and the file holds little or no video | Merges the slices of each picture into one (`hevcmerge`, see below), which raises the limit to 300 pictures. For a picture that cannot be merged it counts the slices and asks the camera for a keyframe (ISAPI `requestKeyFrame`) one second before the limit (`StreamSource._guard_gop`) |

Reolink's mp4 writer (`bc_mp4_*`) runs in the phone app, in Reolink Client (`libBCSDKWrapper.dll`) and in the NVR's backup. The findings come from running the Windows client's copy on frames captured from the proxy (`tools/bc_client.py --raw`).

- **GOP length:** Merged into one slice, a GOP may hold 300 pictures, 12 s at 25 fps, more than the 10 s that H.265+ makes, so no keyframes are requested. Without merging (no library for the CPU, `merge_tiles: false`, or a stream the merger does not support), 3 slices allow 100 pictures, about 3.2 s at 25 fps (6.7 s at 15 fps), and the extra keyframes cost storage. Streams that stay below the limit by themselves are left alone. Only Hikvision cameras that accept `requestKeyFrame` get keyframe requests.
- **The camera's own keyframes:** With H.265+ at 20 fps, the G1 makes a keyframe of its own 282 pictures (14.1 s) after the last keyframe, its own or one asked for. Asking one second before the limit gave its own at picture 282 and the answer 6 pictures later: two keyframes where one was enough. So once two GOPs that ended with a keyframe of the camera's own agree in length (within 2%), and that length fits the limit, nothing is asked while a GOP is within it. Shorter GOPs (H.265+ adds keyframes at a sudden change in the picture) teach nothing. A keyframe that comes within 2 s of a request (from the GOP guard or for a new viewer) counts as its answer, unless another one follows within those 2 s. If a GOP goes past the learnt length, the proxy asks at once and learns again; that GOP can end a few pictures past the limit, and the phone then loses those seconds.
- Recordings made before these fixes still fail to download to the phone. NVR downloads to USB without transcoding were not affected.

## Motion Boxes

**Format:** The boxes travel in the extension area of each video frame, and the client draws them itself. The layout is in `bcproxy/motionbox.py`, from an NVR main stream capture and checked by calling the frame parser in Reolink Client's `libBCSDKWrapper` directly: for the same boxes, its result differs from a real frame's only in the score and class we choose, and a frame without boxes gives the same result as a real one.

**The switch:** Reolink Client shows it once a frame carries this data (`hasAiData` sets `supportAiMark`). No capability bit in 199 is involved. So frames carry the extension only after the camera has sent its first target list, and a camera that sends none shows no option. The mobile app's check is not visible from here.

**Where the boxes come from:** Only from the camera; nothing is computed on the proxy. What Hik-Connect draws comes with the stream of the web preview: a WebSocket on port 7681 (`/ISAPI/Security/token`, then `realplay`), RTP with private packets of payload type 112, the same ones stored in recordings. The packet layout is in `bcproxy/motionbox.py`. Checked on a recording that holds the same boxes twice: all 351 lists of the compressed form agree with the plain one. Drawn on the recording's own frames, the boxes sit on the vehicles.

Places that do not have them: RTSP has only video and audio. Its `isapi.metadata` track (at `/ISAPI/Streaming/channels/101`) gives one sample per target, and an alert's `TargetRect` is also one sample. `httpPreview` answers 403.

| Camera | Live target boxes |
|---|---|
| BackDoor (DS-2CD2087G2-L) | ✅ Seen, on the sub stream |
| MountainView (DS-2CD2347G2-LU) | ✅ Seen. Its sub stream answers `deviceError`, so this reads the main stream |
| CameraTest, MainDoor, Backyard (DS-2CD2347G1-L) | ✅ Seen on CameraTest while somebody walked through, and once on Backyard, in their own form of the list (main stream; the sub stream answers `deviceError`). Not drawn on frames for a check: no recordings on these |
| Road (DS-2CD7526G0-IZHS) | ❌ None seen, 45 s |
| Crossroad (DS-2CD4232FWD-IZH8) | ❌ No session token (HTTP 403) |
| Dahua | ❌ Not looked at; off |

- **Cost:** One more connection to the camera while somebody watches, and an NVR watches all day. No decoding: the proxy only reads packet headers and the target lists. Measured at about 330 kbps on BackDoor's sub stream; a camera falling back to its main stream sends that whole stream to the proxy. On the Cloud Key, 6 cameras' streams cost about 7% of one core, mostly the event loop waking for each TCP segment.
- **Requires:** The camera's WebSocket service on. Turn the feature off with `motion_boxes: false`.
- **Delay:** Boxes and video come over separate connections, so they are not matched frame by frame.
- **Not covered:** Playback has no boxes yet. `ISAPI/ContentMgmt/download` gives the same packets for a recording, which is where to start.

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

**Status:** One real camera (Amcrest `IP4M-1083E-AI`, a rebranded Dahua SD5A432XB-HNR, firmware 2.810, H.265 main stream 2560×1440 at 30 fps, sub stream 640×480 at 15 fps) runs on a real Reolink NVR through the proxy: both streams load and keep running. Reading, preview and PTZ were also verified with `tools/bc_client.py`. An IVS intrusion rule triggered on the camera reaches the proxy as `intrusion`, zone 0, target `people`, with `region_base: 5`, and the NVR sends a notification. Writing settings has not been verified on a real camera. Plain motion is untested on this camera: Motion Detection has to be off for IVS to be on. The rest was only tested with `tools/fake_dahua.py`, and other models and firmware may differ. When connecting a new model for the first time, run with `-v`, and check that the `RuleId` of events matches `region_base` and that `encode.cgi getConfigCaps` yields resolutions (if it cannot be parsed, only the current resolution is offered).

| Feature | Dahua |
|---|---|
| Main/sub stream preview | ✅ Verified on a real NVR |
| Snapshot | ✅ Verified on a real camera (`bc_client`) |
| Audio | 🟡 AAC frames reach the client (`bc_client`), but this camera's own output is nearly silent (about -91 dB), so nothing audible has been confirmed |
| IVS intrusion with a person | ✅ Verified on a real camera and NVR, including the NVR's notification |
| Plain motion, vehicle, line crossing, loitering, tamper | 🟡 Connected, to be verified on a real camera. Motion Detection and IVS cannot be on together on this camera |
| SD card playback | 🟡 Recording search and capacity read on a real camera; RTSP `/cam/playback` to be verified |
| Streams (resolution, frame rate, bitrate, CBR/VBR, audio switch) | 🟡 Capabilities and current values read on a real camera; writing `Encode` to be verified |
| Image page | 🟡 Only brightness, contrast, saturation, sharpness, mirror, flip and day/night; the rest has no effect |
| Tamper Alert switch, renaming | 🟡 Writes `BlindDetect` and `ChannelTitle`, to be verified on a real camera |
| PTZ: direction, zoom, go to preset | ✅ Tested in the app (the zoom slider not confirmed separately) |
| PTZ: save/delete presets, patrols | 🟡 Verified on the Dahua side on a real camera, the app to be verified (see below) |
| PTZ: manual focus | ❌ Not connected |
| Recording schedule and settings (54/55/81/82) | ❌ Not connected and not declared to the app; set them on the Dahua web page |
| Server ports (37/38), manual light, CPU usage, HTTP push events | ❌ Not connected |

Note: Dahua may lock the account after repeated failed logins, and with a wrong password the proxy's reconnects can trigger that. Right after the proxy starts, a single `Dahua status poll failed ... HTTP 401` sometimes appears (5 of 17 starts) and the next poll succeeds. The cause is probably concurrent requests sharing one digest nonce, not yet confirmed.

Two things the NVR needed:

- **A viewer starts at the next keyframe.** Dahua cannot be asked for a new keyframe, and an old one is followed by frames that refer to frames the viewer never got. On this camera the wait is at most about 1 second.
- **The frame rate comes from the camera's `Encode` config.** ffprobe reports double the real value here (60 for a 30 fps main stream, 30 for a 15 fps sub stream).

Which of the two made the stream load on the NVR is not known: both were fixed together.

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

## Development

### Local testing without an NVR

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

### Filling in missing messages from a real camera

1. Export replies from a real Reolink camera:
   `python tools/bc_client.py <camera IP> -p <password> --dump-templates captured/ --get 26 46 56 76 110 112 113 122 132 146 208`
   Add `--nvr-magic` to imitate the NVR's header.
2. Or capture between the NVR and a real camera, then decrypt:
   `python tools/bc_dump.py nvr.pcap -p <camera password> --templates captured/`
   When capturing from a mobile app is impractical, use the relay: `python tools/bc_relay.py <camera IP> --listen 0.0.0.0:9001 --out app.pcap`, add "this PC's IP:9001" by IP in the app, then decrypt `app.pcap` with bc_dump.py.
3. `captured/<id>.xml` takes priority over the built-in templates. Fields such as model and UID can become placeholders like `${model}` and `${uid}`.
4. With `trace: true`, `unhandled msg N` in the log marks messages that still lack a template. Each ID is warned about once per connection.

### Merging the tiles into one slice

`bcproxy/hevcmerge.py` with the C library `bcproxy/native/hevcmerge.c` turns the 3 slices of a picture into one slice that holds the 3 tiles. Nothing is re-encoded, and the pictures decode to the same pixels:

- The 3 slices of a picture carry the same slice header apart from the slice address.
- Each tile starts with freshly initialised CABAC contexts and arithmetic decoder, whether it is its own slice or one tile of a bigger slice, and prediction never crosses a tile boundary.
- In-loop filtering across the old slice boundaries depends on `loop_filter_across_tiles_enabled_flag` alone after the merge. The merge is done only when that gives the same filtering as before.

Two things change in the bitstream. The merged slice header gets entry points (the byte offset of each tile). And each tile except the last one now ends with `end_of_slice_segment_flag` = 0 and `end_of_subset_one_bit` = 1 instead of `end_of_slice_segment_flag` = 1: one more terminate bin before the arithmetic coder flushes, which changes the last bits of the tile. Computing them needs the coder's range at the end of the tile, so the library entropy-decodes every tile: all syntax elements, but no pixels, motion vectors or transforms.

The library supports Main and Main 10, 4:2:0, tiles without WPP, no dependent slice segments and no PPS extensions: what these Hikvision cameras send. Anything else, or a picture that does not decode cleanly, goes out as it came, and the log names the reason once per stream and reason. Each stream merges on a thread of its own, so a big keyframe holds up neither the event loop nor the other cameras, and the tiles of a big picture are decoded in parallel.

Cost on the Cloud Key (Cortex-A53, measured on captures): about 11% of one core for a 1.8 Mbps main stream (Backyard), about 15% at 4.4 Mbps (MainDoor), about 1% for a G1 sub stream. The largest keyframe seen, 826 KB, takes 88 ms; the camera needs about 0.4 s to send it.

Checked on captures from Backyard, Camera23, MainDoor (rotated, 1520×2688) and MountainView (DS-2CD2347G2-LU): every picture merged; ffmpeg decodes the merged streams to the same frames (`framemd5`); the bin sequence of an IDR slice matches libde265's; Reolink's mp4 writer from Reolink Client turns a merged GOP of 250 pictures into an mp4 with every frame, where the unmerged stream gives a file with no video; 6000 corrupted pictures under AddressSanitizer and UBSan neither crash nor leak.

The library is built per CPU and loaded from `bcproxy/native/libhevcmerge-<cpu>.so`. `bcproxy/native/build.sh` builds it for the machine it runs on, `build.sh aarch64` for the Cloud Key (needs `gcc-aarch64-linux-gnu`; `deploy/push.sh` warns when it is missing or older than the source). The Docker image builds it. Without the library (on Windows, for example) nothing is merged. `merge_tiles: false` in `config.yaml` turns merging off.

### Project layout

```
bcproxy/
  crypto.py     BCEncrypt XOR / AES-128-CFB, login hash and key derivation
  bc.py         Baichuan frame codec (header, magic, extension XML, binary mode, FULL_AES)
  bcmedia.py    BcMedia container (InfoV2 / I frame / P frame / AAC), Annex-B access unit splitting
  hevcps.py     H.265 VPS/SPS rewrites for the NVR and for Reolink's mp4 writer
  hevcmerge.py  Merges the tile slices of an H.265 picture into one slice (C library in native/)
  session.py    Server session: login negotiation, template replies, settings writes, alert subscription (31/33), preview (3/4), snapshot (109)
  camera.py     Virtual camera: Hikvision events → state machine → push; capability bits, renaming, Hikvision status polling
  discovery.py  LAN discovery replies (UDP 2000 → 3000)
  hikcfg.py     Settings written to Hikvision: streams, recording schedule, light
  dahua.py      Dahua: event stream, config tables, SD card recordings (same interface as hik/hikcfg/HikRecordings)
  playback.py   Playback: recording search, event tags, replay stream
  settings.py   Merging and persisting settings written by the NVR or app
  events.py     Event state machine + <AlarmEventList> generation
  hik.py        ISAPI: Digest auth, alertStream, multipart parsing, alert mapping, HTTP push receiver
  media.py      ffprobe/ffmpeg pulling RTSP, one ffmpeg shared by all viewers, bitrate statistics, keyframe requests for short GOPs
  logsetup.py   Console and rotating log files, passwords removed before writing
  watchdog.py   systemd readiness notification and watchdog heartbeat, checks that background tasks are alive
  netalias.py   Adds the cameras' listen IPs to the network interface and keeps them there
  templates/    Built-in reply templates (can be overridden by templates_dir)
deploy/         Cloud Key deployment: systemd service, install script for the device, push script for the PC
tools/
  bc_client.py  Baichuan client (tests the proxy / exports templates from a real camera / --raw saves frames)
  bc_dump.py    pcap/pcapng decryption tool
  bc_discover.py LAN search (lists real and virtual cameras)
  bc_relay.py   Recording relay between app/NVR and a real camera (writes a pcap for bc_dump.py)
  fake_hik.py   Fake Hikvision ISAPI service
  fake_dahua.py Fake Dahua CGI service (with PTZ)
tests/          Protocol tests (using real camera samples from neolink) + end-to-end tests: python -m pytest
```

## Technical notes

### ffmpeg hangs

- **stderr must be read all the time:** Otherwise its buffers fill up and ffmpeg blocks writing to stderr. It stops reading the camera, the camera then disconnects, and the process stays alive. The Crossroad sub stream keeps reporting `non monotonically increasing dts`, and with concat input it hung after about 8.5 minutes. Direct input reports fewer of these and hangs more slowly.
- **ffmpeg does not exit when a connection goes silent,** only when it closes. So a stream with no data for `STALL_SECONDS` (20 s) gets a new ffmpeg and a `no data` line in `errors.log`. If that line shows up often, there is a problem with the camera or the network.

### TODO: seeking in playback

Dragging the progress bar still has problems (the exact behavior in the app is yet to be written down). The reply format of 383 is inferred, with no sample from a real camera, and a capture of seeking on a real camera is still missing. To stay under the size limit of one message, the recording list merges segments, and seeking into a gap this creates lands on the next real recording.

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
- **Encryption:** An NVR requests FULL_AES (`0xdc12`) and disconnects at once on a downgrade to AES (0x02). Binary payloads (video, JPEG) are encrypted whole, with `<encryptLen>` in the extension header. Real cameras spell it `<EncryptLen>`, also send `checkPos/checkValue`, and encrypt only the first 1024 bytes. Tried on 2026-11-13: `<encryptLen>1024` with the rest in clear (no checkPos/checkValue, lowercase tag) gave the NVR no picture and stopped its recording, while it kept all previews open. It most likely ignores our lowercase tag and decrypts the whole payload; the capitalised tag alone is not yet tried. So the whole payload stays encrypted, which the NVR and app decode fine.
- **LAN discovery:** Every 2 seconds, an NVR broadcasts the 4 bytes `aa aa 00 00` from UDP 3000 to `255.255.255.255:2000`, and Reolink Client searches the same way. A device answers with a fixed 388-byte structure (IP, port, name, MAC, UID; for field offsets see `bcproxy/discovery.py`). The reply **always goes to the peer's UDP 3000**, whatever the source port of the request. The proxy listens on `0.0.0.0:2000` and answers each search with one packet per virtual camera. `tools/bc_discover.py` lists real and virtual cameras on the LAN.
- **Settings messages:** Settings written by the NVR or app (25/45/57/105/209/297, and pairs of "read N, write N+1") are merged into the matching read replies and saved to `state_dir/<camera>/<id>.xml`. Delete the file to restore the default.
- **Live frame time:** The Reolink app's live view stops for good when the frame time goes back. The time is microseconds since the device started, like a real camera's: the low 32 bits in `micros`, the high half in the last header field (real cameras send 0x1 and 0x47 there). Before, the low half wrapped every 71.6 minutes and the view froze. For testing, `debug_wrap_in` (seconds) puts the next wrap that far ahead.

## Other limitations

- Not implemented: two-way audio, Hikvision PTZ, Motion Boxes in playback, UDP/P2P connections. Playback is video only (no audio), at a fixed 1× speed.
- The image page (25/26/132) reads and writes Hikvision: brightness, contrast, saturation, sharpness, mirror and flip, power frequency, day/night mode, white balance, 3D noise reduction. Exposure, gain, backlight and hue are not connected, and saving them has no effect.
- Server settings (37/38) read and write Hikvision: HTTPS and ONVIF can be changed. Disabling Hikvision's HTTP or RTSP or changing their ports is refused, because the proxy depends on them.
- Settings only stored in the proxy and never written to Hikvision: motion detection (47), OSD, network pages (DHCP, DDNS, UPnP).
- People, vehicle and smart types in playback come from alerts received while the proxy was running (`state/<camera>/events.jsonl`). Hikvision's recording index doesn't distinguish them, so older recordings show only motion.
- Timestamps are the time the proxy received a frame, not the camera's PTS.
- The geometry of smart detection zones (line and polygon coordinates) is not emulated. If the NVR needs to show or edit zones, use 527/529/531 templates captured from a real camera.
- 26 (image parameters) and 208 (LED/IR light) use default templates written with reference to reolink_aio, with fixed values.

## Credits

Protocol details draw on [neolink](https://github.com/QuantumEntangledAndy/neolink) and [reolink_aio](https://github.com/starkillerOG/reolink_aio). The real camera samples in `tests/samples/` come from neolink.
