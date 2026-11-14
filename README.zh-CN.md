# reolink-hik-proxy

[English](README.md) | **简体中文**

把海康威视和大华摄像头伪装成 Reolink 摄像头，让 Reolink NVR 用原生 Reolink 协议（Baichuan, TCP 9000）接入。重点是**事件映射**：人形/车辆识别、越界、区域入侵、徘徊会被翻译成 Reolink 的 AI 和智能侦测事件，而不是 ONVIF 只能传的 motion。

下文未特别说明时讲的都是海康；大华见[大华摄像头](#大华摄像头)。

```
海康 IPC ──ISAPI alertStream / HTTP 推送──┐
        ──RTSP 主/子码流 (ffmpeg -c copy)──┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
        ──ISAPI 抓图──────────────────────┘
```

目录：[快速上手](#快速上手) · [事件映射](#事件映射) · [兼容性状态](#兼容性状态) · [海康 H.265 与 Reolink 下载](#海康-h265-与-reolink-下载) · [Motion Boxes](#motion-boxes) · [大华摄像头](#大华摄像头) · [开发](#开发) · [技术细节](#技术细节)

## 快速上手

### 快速开始

```bash
pip install -r requirements.txt        # cryptography + PyYAML；另需 ffmpeg/ffprobe
cp config.example.yaml config.yaml     # 填海康地址/账号/RTSP、虚拟摄像头的 IP 和密码
python -m bcproxy -c config.yaml -v
```

然后在 Reolink NVR 中手动添加摄像头：填 `listen` 中的 IP，端口 9000，用户名和密码与配置中的 `username` / `password` 一致。

**一台海康对应一个 IP**：NVR 按 IP 区分摄像头。Linux 上可以给代理主机加别名 IP（`ip addr add 192.168.1.201/24 dev eth0`），或用 Docker macvlan；Windows 的做法见下文。

### Windows 上运行

- `listen` 中的 IP 必须是本机网卡上已有的地址。要加别名 IP，网卡需要先改成静态 IP，再执行
  `netsh interface ipv4 add address "以太网" 192.168.0.201 255.255.255.0`。
- 9000 端口可能被 Hyper-V / WSL / Docker 保留（绑定报 `could not bind`）。用 `netsh interface ipv4 show excludedportrange protocol=tcp` 检查；如果被保留，就先 `net stop winnat`，再执行 `netsh int ipv4 add excludedportrange protocol=tcp startport=9000 numberofports=1`，最后 `net start winnat`。
- 防火墙放行入站 TCP 9000 和 UDP 2000（局域网搜索）：
  `New-NetFirewallRule -DisplayName "bcproxy 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow`
  `New-NetFirewallRule -DisplayName "bcproxy search" -Direction Inbound -Protocol UDP -LocalPort 2000 -Action Allow`
- 本机运行 Reolink Client 时，它会占用 UDP 3000，此时 `tools/bc_discover.py` 无法运行（代理本身不受影响）。
- 保存完整日志：`python -m bcproxy -c config.yaml -v *> log.txt`

### 部署到 UniFi Cloud Key Gen2 Plus

在 UCK-G2-Plus（UniFi OS 6.0.11，Debian 13，aarch64）上验证。全部文件都在 `/data/bcproxy`，开机由 systemd 启动。首次安装（Git Bash，在 `~/.ssh/config` 里配好 `UCK-G2-Plus`）：

```bash
bcproxy/native/build.sh aarch64   # 在 WSL 或 Linux 里：为 Cloud Key 的 CPU 编译 H.265 tile 合并库
deploy/push.sh --with-config      # 复制代码、config.yaml、state/，下载 Python 和 ffmpeg，启动服务
deploy/push.sh                    # 以后更新代码：不动设备上的配置和 state/
```

**每台摄像头一个 IP：** NVR 不支持同一 IP 的不同端口。`config.yaml` 里写 `alias_interface: eth0`，并给每台摄像头配 `listen: 192.168.0.5x:9000`，代理启动时把这些 IP 加到网卡上，运行中每 30 秒检查一次，丢失就补回并写入 `errors.log`。不改 `/etc/systemd/network/eth0.network`，这个文件由 UniFi OS 生成。这些 IP 要避开路由器的 DHCP 地址池。NVR 添加摄像头时填对应的 IP，端口 9000。

**日志**（`/data/bcproxy/logs/`，自动轮转，每种最多 6 个 5 MB 的文件）：

| 文件 | 内容 |
|---|---|
| `errors.log` | WARNING 及以上，排查问题先看这里 |
| `bcproxy.log` | 全部日志，级别由 `log_level` 决定 |
| `crash.log` | 致命信号时的 Python 调用栈，含看门狗杀进程时卡住的位置 |
| `restarts.log` | 每次退出的原因：`result=watchdog`、`oom-kill`、`exit-code` 等 |

RTSP 地址和 XML 里的密码写入前会替换成 `***`。

**诊断上传：** UniFi OS 每小时把 `/persistent/report/` 里的报告传给 Ubiquiti，设备上任何进程崩溃时生成的报告都带完整的 `ps`。所以 ffmpeg 的 RTSP 地址（含密码）不放在命令行，而是放进 memfd 里的 ffconcat 文件。`LimitCORE=1` 让代理自己崩溃时不生成报告（0 不行，内核对管道型 `core_pattern` 不看 0）。

常用命令：

```bash
systemctl status bcproxy
tail -f /data/bcproxy/logs/errors.log
journalctl -u bcproxy -b
```

**看门狗：** `Type=notify` + `WatchdogSec=60`。心跳在事件循环里发送，循环卡住 60 秒，或摄像头的后台任务（事件流等）异常退出，服务都会被重启。任何原因退出都在 10 秒后重启，且不限次数。

**固件升级：** 升级时 `overlay_cleanup`（`/usr/share/initramfs-tools/scripts/ubnt-init/apq8053/boot-emmc`）会删除 `/usr`、`/lib`、`/var`（`var/log` 除外）和部分 `/etc`，保留 `/data` 和 `/etc/systemd/system` 里的服务文件。所以这里不用 apt，也不用固件自带的 Python。`ubnt-dpkg-restore` 只恢复官方白名单里的包，打成 deb 没有用。

这份清单是固件脚本的一部分，以后可能变。升级后检查 `systemctl is-enabled bcproxy`；如果服务没了，运行下面的命令恢复，它不需要联网：

```bash
sh /data/bcproxy/app/deploy/install.sh
```

**恢复出厂设置会清空 `/data`**，只能重新运行 `deploy/push.sh --with-config`。`config.yaml` 要在 PC 上留一份；设备上的 `state/` 需要时自己备份。

## 事件映射

| 海康 eventType | 目标 (detectionTarget) | Reolink msg 33 输出 |
|---|---|---|
| `VMD` | 无 | `<status>MD</status>` |
| `VMD`（AcuSense） | human / vehicle | `MD` + `<AItype>people</AItype>` / `vehicle` |
| `linedetection` | human / vehicle | `<smartAiType><type>crossline</type>` + 区域 + 目标，同时上报 AItype 和 MD |
| `fielddetection`、`regionEntrance` | human / vehicle | `intrusion` |
| `loitering` | human / vehicle | `loitering` |
| `shelteralarm`、`tamperdetection` | – | `<status>tamper</status>` |
| `scenechangedetection`（需配置 `event_map: {sceneChangeDetection: tamper}`） | – | `<status>tamper</status>`，App 的 Tamper Alert 开关同时控制遮挡和场景变更 |
| `faceCapture`（JSON 格式，后面跟 JPEG） | face + people | 同时上报人和人脸，这样 NVR 的 Person 推送也会触发；`ai_types` 里没有的类型不上报 |

- 海康 `regionID`（从 1 开始）映射为 Reolink 区域编号 `zone`（从 0 开始）。
- 越界这类单次脉冲告警会保持 `hold.smart` 秒，让 NVR 有时间触发录像；VMD 收到 `inactive` 时立即清除。
- NVR 是否认为摄像头支持这些功能，取决于 msg 199 `<Support>` 中的 `<aitype>`（bit1=人, bit2=车, bit4=猫狗）和 `<smartAI>`（bit1=越界, bit2=入侵, bit3=徘徊）。这两个值根据配置里的 `ai_types` 和 `smart` 自动生成；msg 527/529/531 的区域列表也由配置生成。

## 兼容性状态

在真实 Reolink NVR 和 Reolink 手机 App 上测试过（海康 2688×1520 H.265 主码流 + 640×360 子码流）：

| 功能 | 状态 | 说明 |
|---|---|---|
| 手动添加 / 登录 | ✅ 已验证 | FULL_AES（0x12）加密，回显 NVR 的包头 magic `0x0FEDCBA0` |
| 实时预览（主 / 子码流） | ✅ 已验证 | NVR 和 App 都能看 |
| 手机和 Reolink Client 下载录像 | ✅ 已验证 | 海康 H.265，包括多 tile 的摄像头，清晰和流畅都可以。见[海康 H.265 与 Reolink 下载](#海康-h265-与-reolink-下载) |
| 摄像头改名 | ✅ 已验证 | 保存到 `state_dir`，并同步修改海康通道名称（`hik.sync_name`） |
| AI / 智能侦测事件 | 🟡 代码完成 | msg 31/33；还需要在 NVR 上确认录像触发和图标显示 |
| 码流设置页（56 / 146） | 🟡 待验证 | v7 补了 `videoEncType`，码率改为读取海康的实际配置 |
| 码率 / CPU 占用 | 🟡 待 NVR 确认 | 122 回复 `<PerformanceInfo>`（`cpuUseRate`、`codeRate`、`netDataRate`），消息号和字段来自 Reolink Client 的 SDK；码率单位是字节/秒（Reolink 摄像头固件如此），NVR 显示时 ×8/1000 换成 kbps；大华拿不到 CPU，显示 0 |
| App 设置页 | 🟡 待验证 | 37 端口、42 邮件、46 移动侦测、52 隐私遮挡、76 网络、112 码流、120 在线用户、132 图像，格式按真机抓包 |
| 码流分辨率 | 🟡 待 App 验证 | 146 列出海康提供的全部主码流分辨率（子码流和真机一样不可选）；57 写入后改海康的分辨率、帧率、码率并重连视频。App 发的 `videoEncType`、`encoderType` 不可信，不处理 |
| 录像设置页 | 🟡 待 App 验证 | 81/82 ↔ 海康录像计划（Normal=CMR，其余类型=EDR），54/55 ↔ 覆盖、预录、延录 |
| 高级网络设置页 | 🟡 待 App 验证 | 40/97/210/455，只保存在代理，不改海康网络 |
| Tamper Alert | 🟡 待 App 验证 | 199 `aitype` 第 25 位声明能力，763/764 读写海康遮挡侦测开关；海康 `shelteralarm` 上报为 `tamper` |
| 手动灯 | 🟡 待 App 验证 | 288 → 海康 supplementLight，`light_seconds`（默认 30 秒）后恢复原模式，291 推送状态 |
| 声音 | 🟡 待 App 验证 | 主码流音频转码为 16 kHz AAC，随预览发送（仅预览；海康无喇叭，不支持对讲） |
| Motion Boxes | 🟡 待 App 验证 | 只在实时预览，用相机自己的目标框，只在发这种数据的相机上生效。端到端验证到 Reolink Client 自己的解析器；屏幕显示和经 NVR 转发还没看到。详见下文 |
| 回放 | 🟡 待验证 | 海康 SD 卡：ISAPI 录像搜索 + RTSP 回放（只播所选的一段，播完停止）→ 102/142/14/15/16/381/382，拖动 383；海康无存储时自动关闭。298 时间轴缩略图未实现 |
| 局域网自动搜索 | 🟡 代码完成 | UDP 2000，回复格式按真机抓包；已确认 NVR 的搜索包能收到并回复，NVR“添加摄像头”列表中的显示还待确认 |
| 其他消息 | ❌ 未知 | 110、113、122 等回空 200；257 回 400、789/818 回 405（与真机一致） |

### 性能

实测（2.0 Mbps 主码流）：解析和封装加密约 0.16% 单核，ffmpeg 转发约 2% 单核，音频转码约 1.6% 单核。CPU 不是瓶颈，首帧等待（1.3～2 秒）主要花在 RTSP 和 ffmpeg 分析上。`-fflags nobuffer`、小 `-probesize` 在主码流和回放上出不了画面，不要用。

Cloud Key Gen2 Plus（8 核 Cortex-A53，2.0 GHz）上带 8 台摄像头，NVR 全天看所有主码流和子码流，6 台开 Motion Boxes，一路麦克风：Python 约占 47% 单核，17 个 ffmpeg 合计约 29%，整机 80% 以上空闲。代理是单个 asyncio 进程，上限是一个核，现在的负载还有一倍余量。py-spy 采样看，主要开销是视频的 AES（FULL_AES 整段加密，见技术说明）、事件循环为 ffmpeg 管道和 Motion Boxes 连接的每个 TCP 段醒一次（数据实时到达，一次一个段），以及 H.265/H.264 解析。解析器每个字节只扫一次，读 NAL 头时不再复制整个 NAL；Motion Boxes 按整块读取解析，不再一帧一帧地等；每个 ffmpeg 用一个看门狗代替每次读取的超时。这些改动让代理从 53% 单核降到 47%。

## 海康 H.265 与 Reolink 下载

有些海康型号把每帧画面分成几个 tile，每个 tile 单独一个 slice：DS-2CD2347G1-L 和 DS-2CD2347G2-LU 的主码流是 3 个，G1 的子码流是 2 个。Reolink 自己的摄像头每帧只有 1 个 slice，Reolink 的部分代码处理不了更多。代理不重新编码，而是绕开这些问题：改写后的码流用 ffmpeg 解码，画面和原来逐帧相同（用 `framemd5` 核对过）。

| 问题 | 后果 | 代理的处理 |
|---|---|---|
| VPS/SPS 声明 2 个时域子层（G1 关掉 SVC 也是这样） | NVR 的解码器改用 SVC 参考帧处理，P 帧解码失败 | 把 VPS/SPS 改写成单层（`hevcps.single_sub_layer`），只在没有参考帧集合保留多于 1 个短期参考帧时才改 |
| VPS/SPS/PPS 超过 64 字节（含起始码）。G1 的 SPS 是 64 字节，H.265+ 下 81 字节 | Reolink 的 mp4 封装代码直接放弃：手机 App 提示下载错误，NVR 备份出空文件 | 去掉 SPS 里 VUI 的 HRD 参数（`hevcps.fit_mp4_limit`），变成 47 字节。解码用不到这些参数，摄像头也不发引用它们的 SEI |
| 一个 GOP 用一次 `writev()` 写入，最多 600 片，每个 NAL 占 2 片，所以 GOP 帧数 × slice 数不能超过 300 | 整个 GOP 被丢掉（`v_io_count to big:600`），文件里几乎没有视频 | 把每帧的几个 slice 合并成一个（`hevcmerge`，见下文），上限变成 300 帧。合并不了的帧，仍按 slice 数在上限前 1 秒向摄像头请求关键帧（ISAPI `requestKeyFrame`，见 `StreamSource._guard_gop`） |

Reolink 的 mp4 封装代码（`bc_mp4_*`）在手机 App、Reolink Client（`libBCSDKWrapper.dll`）和 NVR 的备份里都有。以上结论来自用 Windows 客户端里的这份代码转换代理发出的帧（帧用 `tools/bc_client.py --raw` 抓取）。

- **GOP 长度：** 合并成一个 slice 后，一个 GOP 最多 300 帧，25 fps 下 12 秒，比 H.265+ 的 10 秒还长，所以不用请求关键帧。不合并时（没有对应 CPU 的库、`merge_tiles: false`，或合并模块不支持的码流），3 个 slice 只允许 100 帧：25 fps 下约 3.2 秒（15 fps 下 6.7 秒），多出来的关键帧会多占硬盘。本来就不超限的码流不受影响。只有支持 `requestKeyFrame` 的海康摄像头会收到关键帧请求。
- **摄像头自己的关键帧：** G1 开 H.265+、20 fps 时，在上一个关键帧（不管是它自己的还是被请求出来的）之后 282 帧（14.1 秒）自己出一个关键帧。在上限前 1 秒请求的话，第 282 帧来它自己的，6 帧后又来请求的回应：本来一个就够，结果出了两个。所以只要有两个以摄像头自己的关键帧结束的 GOP 长度一致（误差 2% 以内），而且这个长度在上限内，GOP 没超过这个长度时就不请求。更短的 GOP（H.265+ 在画面突变时会额外插关键帧）不参与学习。请求（GOP 保护发的，或新观看者触发的）之后 2 秒内来的关键帧算作回应，除非 2 秒内又来了一个。如果某个 GOP 超过了学到的长度，代理立刻请求并重新学习；这个 GOP 可能超出上限几帧，手机下载会丢掉这几秒。
- 修复之前录下的录像，手机仍然下载不了。NVR 不转码直接下载到 U 盘不受影响。

## Motion Boxes

**格式：** 框放在每个视频帧的扩展区里，由客户端自己绘制。布局见 `bcproxy/motionbox.py`，来自 NVR 主码流抓包，并直接调用 Reolink Client `libBCSDKWrapper` 里的帧解析函数核对过：同样的框，解析结果和真机帧只差我们自己选的分数和类别；没有框的帧，解析结果和真机完全相同。

**开关：** Reolink Client 只要收到带这种数据的帧就会显示开关（`hasAiData` 会设置 `supportAiMark`），和 199 的能力位无关。所以相机发出第一个目标列表后，帧才带扩展区；从不发目标的相机不会出现开关。手机 App 的判断方式这里看不到。

**框从哪来：** 只来自相机，代理上不做任何计算。Hik-Connect 画的框随网页预览的码流一起发：WebSocket 7681 端口（先取 `/ISAPI/Security/token`，再发 `realplay`），RTP 里的私有包（payload type 112），和录像里存的是同一种。包的布局见 `bcproxy/motionbox.py`。用一段同时存了两份框的录像核对过：压缩形式的 351 个列表全部和明文一致。画回录像自己的帧上，框都落在车上。

这些地方没有：RTSP 只有视频和音频。它的 `isapi.metadata` 轨（在 `/ISAPI/Streaming/channels/101`）每个目标只有一个样本，告警里的 `TargetRect` 也只有一个样本。`httpPreview` 返回 403。

| 相机 | 实时目标框 |
|---|---|
| BackDoor（DS-2CD2087G2-L） | ✅ 已看到，走子码流 |
| MountainView（DS-2CD2347G2-LU） | ✅ 已看到。子码流返回 `deviceError`，所以读主码流 |
| CameraTest、MainDoor、Backyard（DS-2CD2347G1-L） | ✅ 有人在 CameraTest 画面里走动时看到了，Backyard 也出现过一次，用的是它们自己的列表格式（主码流；子码流返回 `deviceError`）。没有画回帧上核对：这几台没有录像 |
| Road（DS-2CD7526G0-IZHS） | ❌ 45 秒内没看到 |
| Crossroad（DS-2CD4232FWD-IZH8） | ❌ 拿不到会话 token（HTTP 403） |
| 大华 | ❌ 没有查，已关闭 |

- **成本：** 有人观看时多一条到相机的连接，而 NVR 全天都在看。不解码：代理只读包头和目标列表。BackDoor 子码流约 330 kbps；回退到主码流的相机，会把整路主码流发到代理。Cloud Key 上 6 台摄像头的这条连接约占 7% 单核，主要是事件循环为每个 TCP 段醒一次。
- **依赖：** 相机的 WebSocket 服务要开着。用 `motion_boxes: false` 关闭此功能。
- **延迟：** 框和视频走不同的连接，没有逐帧对齐。
- **没有覆盖：** 回放还没有框。`ISAPI/ContentMgmt/download` 能拿到录像里同样的包，可以从这里开始。

## 大华摄像头

配置里把 `hik:` 换成 `dahua:` 即可，示例见 `config.example.yaml`。

```
大华 IPC ──eventManager.cgi 事件流─────────────┐
        ──RTSP 主/子码流 (ffmpeg -c copy)──────┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
        ──snapshot / configManager / mediaFileFind┘
```

| 大华事件 Code | 目标 | Reolink msg 33 输出 |
|---|---|---|
| `VideoMotion` | 无 | `MD` |
| `SmartMotionHuman` / `SmartMotionVehicle` | 由 Code 决定 | `MD` + `people` / `vehicle` |
| `CrossLineDetection` | `Object.ObjectType` | `crossline` |
| `CrossRegionDetection` | `Object.ObjectType` | `intrusion` |
| `WanderDetection` | `Object.ObjectType` | `loitering` |
| `VideoBlind` | – | `tamper` |

大华的 `Start` / `Stop` 只各发一次，代理在两者之间每秒重复一次事件，效果和海康的持续告警相同；超过 10 分钟没等到 `Stop` 则认为丢失。除普通移动外，`Stop` 不会立刻清除状态，而是等 `hold` 保持时间结束。

**状态：** 一台真机（Amcrest `IP4M-1083E-AI`，即大华 SD5A432XB-HNR，固件 2.810，H.265 主码流 2560×1440 30 fps，子码流 640×480 15 fps）已通过代理接入真实的 Reolink NVR，两路码流都能加载并持续运行。读取、预览和 PTZ 也用 `tools/bc_client.py` 验证过。在摄像头上触发 IVS 入侵规则，代理收到后上报为 `intrusion`、区域 0、目标 `people`（`region_base: 5`），NVR 有推送。写入设置还没有在真机上验证。普通移动在这台摄像头上未经测试：要开启 IVS，就得关闭移动侦测。其余部分只用 `tools/fake_dahua.py` 测试过，不同型号和固件可能有差异。第一次接新型号时用 `-v` 运行，重点看事件的 `RuleId` 是否对应 `region_base`，以及 `encode.cgi getConfigCaps` 能否解析出分辨率（解析不出时只提供当前分辨率）。

| 功能 | 大华 |
|---|---|
| 主/子码流预览 | ✅ 真实 NVR 验证 |
| 抓图 | ✅ 真机验证（`bc_client`） |
| 声音 | 🟡 AAC 帧能到达客户端（`bc_client`），但这台摄像头输出的音频几乎是静音（约 -91 dB），还没有确认过实际声音 |
| IVS 入侵（人） | ✅ 真机和 NVR 验证，NVR 有推送 |
| 普通移动、车辆、越界、徘徊、遮挡 | 🟡 已接，待真机验证。这台摄像头的移动侦测和 IVS 不能同时开启 |
| SD 卡回放 | 🟡 录像查询和容量已在真机读到；RTSP `/cam/playback` 回放待验证 |
| 码流（分辨率、帧率、码率、CBR/VBR、声音开关） | 🟡 能力和当前值已在真机读到；写入 `Encode` 待验证 |
| 图像页 | 🟡 只接亮度、对比度、饱和度、锐度、镜像、翻转、日夜模式；其余不生效 |
| Tamper Alert 开关、改名 | 🟡 写到 `BlindDetect`、`ChannelTitle`，待真机验证 |
| PTZ：方向、变焦、转到预置点 | ✅ App 已测（变焦滑块未单独确认） |
| PTZ：保存/删除预置点、巡航 | 🟡 大华侧已在真机验证，App 待验证（见下） |
| PTZ：手动对焦 | ❌ 未接 |
| 录像计划和录像设置（54/55/81/82） | ❌ 未接，不向 App 声明；请在大华网页上设置 |
| 服务端口（37/38）、手动灯、CPU 占用、HTTP 推送事件 | ❌ 未接 |

注意：大华连续登录失败可能锁定账号，密码填错时代理的重连可能触发锁定。代理刚启动时偶尔会出现一条 `Dahua status poll failed ... HTTP 401`（17 次启动中有 5 次），下一次轮询就会成功。原因可能是并发请求共用一份 Digest nonce，尚未确认。

NVR 需要的两点：

- **新观看者从下一个关键帧开始。** 大华不能请求新的关键帧，而旧关键帧后面的帧引用了观看者没收到过的帧。这台摄像头最多多等约 1 秒。
- **帧率取自摄像头的 `Encode` 配置。** 这里 ffprobe 报的是实际值的两倍（30 fps 的主码流报 60，15 fps 的子码流报 30）。

这两处是一起修的，不确定是哪一处让 NVR 能加载视频流。

### PTZ

| Baichuan 消息 | 大华 |
|---|---|
| 18 `PtzControl`（left/right/up/down/斜向/stop、速度 1–64） | `ptz.cgi` start/stop，速度换算为 1–8 |
| 18 变焦（zoomIn/zoomInc、zoomOut/zoomDec） | `ZoomTele` / `ZoomWide` |
| 190 / 19 `toPos`、`setPos`、`delPos` | `getPresets` / `GotoPreset`、`SetPreset` + `PtzPreset[].Name`、`ClearPreset`（只显示和操作 1–128 号） |
| 18 `startPatrol` / `stopPatrol` + `patrolId` | `StartTour` / `StopTour` |
| `<PtzCruise>`（写入按标签识别，读取的消息号见下） | `PtzTour` 配置表 |
| 294 / 295 `zoomPos` | `ZoomMapValue`（0–16384）/ 见下 |

- **依据：** `ptzType=2`、`ptzPreset` 来自 reolink_aio；命令名和 `PtzCruise`、`PtzPreset` 的字段来自 Reolink Client 的 `libBCSDKWrapper.dll`。`ptzMode=pt`、`ptzControl`、`ptzCfg` 没有真机抓包。App 发来不认识的命令时，日志会打印 `PTZ msg ... failed` 和完整 XML，按它补 `dahua.PTZ_CODES`。
- **安全：** 移动时客户端断开会立即停止；超过 `ptz_max_seconds`（默认 30 秒）没收到 stop 也会停止；停止失败重试 3 次。代理被强杀时无法发送停止命令。
- **预置点：** App 只看到 1–128 号，保存和删除也只接受这个范围，之后的预置点留在摄像头上不动。保存到已有编号会覆盖它。
- **巡航：** 对应大华 Tour，只列出有预置点的路线，数量上限取 `caps.TourMax`。这台机的驻留时间最少 15 秒，写入更小的值会被抬到 15。
- **巡航的两套编号（真机确认）：** App 的 `patrolId` 从 0 开始，大华 Tour 从 1 开始。`PtzTour` 表里的预置点是从 0 开始的序号（预置点 7 存成 6，`-1` 表示空），写错了不会报错，摄像头只会去错位置。
- **巡航读取：** App 打开 PTZ 面板时会依次发 294、190、64。64 返回空内容时，面板提示 “failed to load”，所以按巡航读取回复 `<PtzCruise>`（`session.MSG_CRUISE_READS`）。
- **绝对变焦：** 这台机的 `PositionABS` 不能设置变焦，每次调用都只放大一档，与 `arg3` 无关，所以 295 用定时脉冲加读回修正（`ZOOM_RATE`）。实测广角到中段误差约 70，长焦端约 600（满量程 16384）。速度参数对变焦几乎没有影响。
- `getStatus` 的 `MoveStatus` 在转到预置点时会过早显示 Idle，判断到位要看位置是否稳定。
- 摄像头自己的归位任务（PtzAutoMovement）不受代理控制，可能在 App 操作后把画面转回去。

## 开发

### 不连 NVR 的本地验证

```bash
# 终端 1：假海康（Digest 认证 + alertStream + 抓图），可交互输入事件：vmd human / line 1 vehicle / field 2 human
python tools/fake_hik.py --port 8080
# 终端 2：代理（hik.host 填 127.0.0.1:8080，main_rtsp 可以是本地 mp4 文件）
python -m bcproxy -c config.yaml -v
# 终端 3：Baichuan 客户端，订阅告警并打印 msg 33
python tools/bc_client.py 127.0.0.1 -p change-me --get 199 527 --alarms --seconds 60
python tools/bc_client.py 127.0.0.1 -p change-me --preview subStream --out sub.h264 --snap a.jpg
```

大华用 `python tools/fake_dahua.py --port 8081`（账号 `admin` / `dahua12345`），可输入 `motion`、`human`、`human off`、`line 1 Human`、`region 2 Vehicle`、`blind`。

### 用真机数据补全未适配的消息

1. 用真 Reolink 摄像头导出回复：
   `python tools/bc_client.py <真机IP> -p <密码> --dump-templates captured/ --get 26 46 56 76 110 112 113 122 132 146 208`
   加上 `--nvr-magic` 可以模拟 NVR 的包头。
2. 或者在 NVR 和真机之间抓包，然后解密：
   `python tools/bc_dump.py nvr.pcap -p <摄像头密码> --templates captured/`
   手机 App 不方便抓包时，可以用中继：`python tools/bc_relay.py <真机IP> --listen 0.0.0.0:9001 --out app.pcap`，然后在 App 里用 IP 添加“本机IP:9001”，再用 bc_dump.py 解密 `app.pcap`。
3. `captured/<id>.xml` 会优先于内置模板使用，型号、UID 等字段可以改成 `${model}`、`${uid}` 这样的占位符。
4. 代理开启 `trace: true` 时，日志里的 `unhandled msg N` 就是还缺模板的消息。每个连接里同一个 ID 只警告一次。

### 把 tile 合并成一个 slice

`bcproxy/hevcmerge.py` 和 C 库 `bcproxy/native/hevcmerge.c` 把一帧的 3 个 slice 变成一个包含 3 个 tile 的 slice。不重新编码，解码出的像素完全相同：

- 一帧的 3 个 slice，slice 头除了 slice 地址以外都一样。
- 每个 tile 开头都会重新初始化 CABAC 上下文和算术解码器，不管它是单独一个 slice，还是大 slice 里的一个 tile；预测也从不跨越 tile 边界。
- 合并后，原来 slice 边界上的环路滤波只由 `loop_filter_across_tiles_enabled_flag` 决定。只有滤波结果和合并前相同时才合并。

码流里有两处变化。合并后的 slice 头多了入口点（每个 tile 的字节偏移）。除最后一个 tile 外，每个 tile 的结尾从 `end_of_slice_segment_flag` = 1 变成 `end_of_slice_segment_flag` = 0 加 `end_of_subset_one_bit` = 1：算术编码器结束前多了一个终止 bin，tile 最后几个 bit 随之改变。要算出这几个 bit，需要知道编码器在 tile 末尾的 range，所以库会对每个 tile 做熵解码：读出全部语法元素，但不重建像素，不推导运动矢量，也不做反变换。

库支持 Main 和 Main 10、4:2:0、不带 WPP 的 tile、不含依赖 slice 段、不含 PPS 扩展，正是这些海康摄像头发的码流。其他情况，或者某帧解码不干净，就按原样发出，日志里每路码流每种原因记一次。每路码流在自己的线程里合并，大的关键帧既不会卡住事件循环，也不会耽误其他摄像头；大帧的几个 tile 并行解析。

Cloud Key 上的开销（Cortex-A53，用录像实测）：1.8 Mbps 的主码流（Backyard）约占 11% 单核，4.4 Mbps（MainDoor）约 15%，G1 子码流约 1%。见过的最大关键帧 826 KB，合并用 88 ms；摄像头发送这一帧本身就要约 0.4 秒。

用 Backyard、Camera23、MainDoor（旋转画面，1520×2688）和 MountainView（DS-2CD2347G2-LU）的录像验证过：每帧都合并成功；ffmpeg 解码合并后的码流，逐帧和原来相同（`framemd5`）；一个 IDR slice 的 bin 序列和 libde265 完全一致；用 Reolink Client 里 Reolink 的 mp4 封装代码转换 250 帧的 GOP，合并后的 mp4 一帧不少，未合并的码流转出来没有视频；在 AddressSanitizer 和 UBSan 下喂 6000 帧被破坏的数据，不崩溃也不泄漏内存。

库按 CPU 分别编译，从 `bcproxy/native/libhevcmerge-<cpu>.so` 加载。`bcproxy/native/build.sh` 为当前机器编译，`build.sh aarch64` 为 Cloud Key 编译（需要 `gcc-aarch64-linux-gnu`；库不存在或比源码旧时，`deploy/push.sh` 会提醒）。Docker 镜像会自动编译。没有库时（例如在 Windows 上）不合并。库对系统只需要 glibc 2.17 的符号，和加载它的 Python 一样，而且和代理的其他文件一起放在 `/data` 下，固件更新后仍能使用。万一加载失败，日志里记一次，合并关闭，代理照常运行，像以前一样请求关键帧。在 `config.yaml` 里写 `merge_tiles: false` 可以关掉合并。

### 文件结构

```
bcproxy/
  crypto.py     BCEncrypt XOR / AES-128-CFB，登录哈希和密钥派生
  bc.py         Baichuan 帧编解码（头部、magic、扩展 XML、二进制模式、FULL_AES）
  bcmedia.py    BcMedia 容器（InfoV2 / I 帧 / P 帧 / AAC），Annex-B 访问单元切分
  hevcps.py     H.265 VPS/SPS 改写（给 NVR 和 Reolink 的 mp4 封装代码）
  hevcmerge.py  把一帧 H.265 的多个 tile slice 合并成一个 slice（C 库在 native/）
  session.py    服务端会话：登录协商、模板应答、设置写入、告警订阅(31/33)、预览(3/4)、抓图(109)
  camera.py     虚拟摄像头：海康事件 → 状态机 → 推送；能力位、改名、海康状态轮询
  discovery.py  局域网搜索应答（UDP 2000 → 3000）
  hikcfg.py     写到海康的设置：码流、录像计划、灯光
  dahua.py      大华：事件流、配置表、SD 卡录像（接口和 hik/hikcfg/HikRecordings 对应）
  playback.py   回放：录像搜索、事件标签、回放流
  settings.py   NVR/App 写入设置的合并与持久化
  events.py     事件状态机 + <AlarmEventList> 生成
  hik.py        ISAPI：Digest 认证、alertStream、multipart 解析、告警映射、HTTP 推送接收
  media.py      ffprobe/ffmpeg 拉 RTSP，多观看者共享一个 ffmpeg，码率统计，为缩短 GOP 请求关键帧
  logsetup.py   控制台和轮转日志文件，写入前去掉密码
  watchdog.py   systemd 就绪通知和看门狗心跳，检查后台任务是否还活着
  netalias.py   把摄像头的 listen IP 加到网卡上并保持
  templates/    内置回复模板（可被 templates_dir 覆盖）
deploy/         Cloud Key 部署：systemd 服务、设备上的安装脚本、PC 上的推送脚本
tools/
  bc_client.py  Baichuan 客户端（测试代理 / 从真机导出模板 / --raw 保存原始帧）
  bc_dump.py    pcap/pcapng 解密工具
  bc_discover.py 局域网搜索（列出真机和虚拟摄像头）
  bc_relay.py   App/NVR 与真机之间的录制中继（输出 pcap，交给 bc_dump.py 解密）
  fake_hik.py   假海康 ISAPI 服务
  fake_dahua.py 假大华 CGI 服务（含 PTZ）
tests/          协议测试（使用 neolink 的真机样本）+ 端到端测试：python -m pytest
```

## 技术细节

### ffmpeg 卡死

- **stderr 要一直读：** 不读的话缓冲区写满，ffmpeg 就阻塞在写 stderr 上，不再读摄像头，摄像头随后断开，而进程还活着。Crossroad 子码流会持续报 `non monotonically increasing dts`，用 concat 输入时约 8 分半就卡死，直连输入报得少，卡死得慢。
- **连接静默时 ffmpeg 不会退出：** 只有连接被关闭才会。所以 `STALL_SECONDS`（20 秒）内没有数据就重启 ffmpeg，并在 `errors.log` 记一条 `no data`。这条经常出现，说明摄像头或网络有问题。

### TODO：回放拖动

拖动进度条还有问题（App 上的具体表现待整理）。383 的回复格式是推断的，没有真机样本；真机上拖动时的抓包还没拿到。录像列表为了不超过单条消息上限会合并片段，拖到合并产生的空档会落到下一段真实录像。

### 协议上踩过的坑

- **同一连接的并发：** 读循环不等请求处理完，否则慢请求期间连 socket 都不读，PTZ 的 `stop` 进不了处理。登录除外：下一条消息的解密依赖它。
- **回放筛选类型：** 14 的 `<recordType>` 要列出 `crossline,intrusion,loitering`；片段的 `recordType` 保留 `md`，只有 `sched` 在有其他类型时才去掉。199 的 `aitype` 第 0 位是 Motion：Reolink Client 的 SDK 里，`GetSupportMDEvent` 读这一位，`GetSupportPeopleEvent` 读同一字段的第 1 位。
- **通知里的事件类型：** Tamper 是 `aitype` 第 13 位（第 25 位只对应设置页开关），越界、警戒区和徘徊是 `smartAI` 第 1–3 位。
- **没有人车识别的摄像头：** `aitype` 不声明人和车、只声明越界和入侵时，NVR 的通知设置页排版会错乱（DS-2CD2347G1-L 真机确认）。所以这种摄像头也配 `ai_types: [people, vehicle]` 和 `targets: [people, vehicle]`，页面上的 Person、Vehicle 选项不会触发；`untargeted_smart_target` 保持不设，越界和入侵不会被当成人上报。
- **不要声明 `eventTypeVersion`：** 199 带这个字段时，回放筛选会多出 Answered Call、Crying Sound 这类摄像头没有的事件（这些名称是 Reolink 客户端自带的）。去掉后这些事件消失，NVR 的通知设置里越界和入侵仍在（BackDoor 真机确认）。
- **灯和红外按钮：** 白光灯来自 199 的 `ledCtrl` 和 `lightType`。151 的 `ledState` 是 LED/红外控制，声明它会多出红外按钮，所以不声明。海康的白光灯通常在 `supplementLight`；DS-2CD2347G1-L 没有这个接口（HTTP 403），开关在 `/ISAPI/System/externalDevice` 的 `SupplementLight`。它的 `auto` 模式白天不亮，所以开灯时改成全天 `schedule` 和手动亮度，关灯时写回原配置。
- **告警推送：** 日志里真实的 NVR 和手机 App 登录后都没有发过 31（订阅），所以 33 推给所有已登录的连接，不等订阅。

- **回放切换文件**：真机只发一次 `replayStreamInfo`，下一个文件开始时帧头最后一个字段（文件编号）变化，`micros` 换新基准，I 帧带该文件的真实时间，没有其他消息（Duo 2 抓包）。代理目前不做自动切换，这里只作记录。若要做，海康的一条 RTSP 流能无缝跨过录像空档，需加 `-use_wallclock_as_timestamps 1` 避免时间戳报错。
- **回放片段结束**：海康的 RTSP 回放到 `endtime` 后既不发帧也不断开，要靠超时判断结束。
- **单条消息大小**：App 收到超过约 40 KB 的消息会卡住（真机单条最大 39488 字节），所以录像列表要控制在这个大小以内。
- **码率模式**：App 的 Bitrate Mode 不走 57，而是图像消息 25 里的 `encType`（真机抓包），对应海康两路码流的 CBR/VBR。
- **能力位**：App 按 199 的字段决定显示哪些选项，字段缺失不等于不支持。`binoCfg` 要明确写 0，否则会出现 Duo 系列才有的 Sync Brightness。`newIspCfg` 各位含义：0 日夜、1 防闪烁、2 曝光、8 亮度、9 对比度、10 饱和度、11 色调、12 锐度（来自 reolink_aio）。这里用 5891，只声明已接到海康的项。
- **包头 magic**：Reolink NVR 用 `0x0FEDCBA0`（`a0 cb ed 0f`），App 和 neolink 用 `0x0ABCDEF0`。摄像头必须用对端发来的那个 magic 回复，否则 NVR 会丢掉回复，约 200 ms 后断开，界面上显示为“连接超时”。
- **加密**：NVR 请求 FULL_AES（`0xdc12`），降级到 AES（0x02）会被直接断开。二进制载荷（视频、JPEG）整段加密，扩展头里带 `<encryptLen>`。真机写成 `<EncryptLen>`，还会带 `checkPos/checkValue`，并且只加密前 1024 字节。2026-11-13 试过：`<encryptLen>1024`、其余明文（不带 checkPos/checkValue，小写标签），NVR 一直开着所有预览，却没有画面，录像也停了。很可能它不认我们的小写标签，整段都当密文解；只改成大写标签还没试过。所以仍然整段加密，NVR 和 App 都能正常解码。
- **局域网搜索**：NVR 每 2 秒从 UDP 3000 向 `255.255.255.255:2000` 广播 4 字节 `aa aa 00 00`，Reolink Client 也这样搜索。设备回复一个 388 字节的定长结构（IP、端口、名称、MAC、UID，字段偏移见 `bcproxy/discovery.py`）。回复**固定发到对方的 UDP 3000**，与请求的源端口无关。代理在 `0.0.0.0:2000` 上监听，每收到一次搜索，就为每台虚拟摄像头各回一个包。`tools/bc_discover.py` 可以列出局域网里的真机和虚拟摄像头。
- **设置消息**：NVR/App 写入的设置（25/45/57/105/209/297，以及“读 N、写 N+1”这种配对）会合并到对应的读取回复里，保存到 `state_dir/<camera>/<id>.xml`，删掉文件即可恢复默认。
- **实时预览的帧时间**：帧时间一旦倒退，Reolink App 的实时预览就会永久卡住。帧时间用设备开机以来的微秒数，和真机一样：低 32 位放在 `micros`，高位放在帧头最后一个字段（真机在这里发 0x1、0x47）。以前低 32 位每 71.6 分钟回绕一次，预览就会卡住。测试时可以用 `debug_wrap_in`（秒）把下一次回绕提前到这么久之后。

## 其他限制

- 未实现：对讲、海康 PTZ、回放里的 Motion Boxes、UDP/P2P 连接。回放只回放视频（无音频），播放速度固定 1 倍。
- 图像页（25/26/132）读写海康：亮度、对比度、饱和度、锐度、镜像翻转、电源频率、日夜模式、白平衡、3D 降噪。曝光、增益、背光、色调没有对接，保存后不生效。
- 服务器设置（37/38）读写海康：HTTPS、ONVIF 可改；禁用海康 HTTP/RTSP 或改它们的端口会被拒绝，因为代理依赖它们。
- 只保存在代理、不会写到海康的设置：移动侦测（47）、OSD、网络类页面（DHCP、DDNS、UPnP）。
- 回放的人/车/智能类型来自代理运行期间收到的告警（`state/<camera>/events.jsonl`）；海康录像索引本身不区分，所以更早的录像只显示移动。
- 时间戳使用代理收到帧的时间，不是摄像头 PTS。
- 智能侦测区域的几何信息（线、多边形坐标）没有模拟。如果 NVR 需要显示或编辑区域，请使用真机抓到的 527/529/531 模板。
- 26（图像参数）和 208（LED/红外灯）用的是参考 reolink_aio 写的默认模板，数值是固定的。

## 致谢

协议细节参考了 [neolink](https://github.com/QuantumEntangledAndy/neolink) 和 [reolink_aio](https://github.com/starkillerOG/reolink_aio)，`tests/samples/` 中的真机样本来自 neolink。
