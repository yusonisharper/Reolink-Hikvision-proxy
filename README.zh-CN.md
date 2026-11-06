# reolink-hik-proxy

[English](README.md) | **简体中文**

把海康威视和大华摄像头伪装成 Reolink 摄像头，让 Reolink NVR 用原生 Reolink 协议（Baichuan, TCP 9000）接入。重点是**事件映射**：人形/车辆识别、越界、区域入侵、徘徊会被翻译成 Reolink 的 AI 和智能侦测事件，而不是 ONVIF 只能传的 motion。

下文未特别说明时讲的都是海康；大华见[大华摄像头](#大华摄像头)。

```
海康 IPC ──ISAPI alertStream / HTTP 推送──┐
        ──RTSP 主/子码流 (ffmpeg -c copy)──┼─▶ bcproxy ──Baichuan :9000──▶ Reolink NVR
        ──ISAPI 抓图──────────────────────┘
```

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

**状态：** 一台真机（Amcrest `IP4M-1083E-AI`，即大华 SD5A432XB-HNR，固件 2.810，H.265 主码流 2560×1440 30 fps，子码流 640×480 15 fps）已通过代理接入真实的 Reolink NVR，两路码流都能加载并持续运行。读取、预览和 PTZ 也用 `tools/bc_client.py` 验证过。写入设置还没有在真机上验证；移动、人/车和智能事件也还没见过：事件流能连上，但接入 NVR 的约 3 小时内没有收到任何事件，所以这部分未经测试。其余部分只用 `tools/fake_dahua.py` 测试过，不同型号和固件可能有差异。第一次接新型号时用 `-v` 运行，重点看事件的 `RuleId` 是否对应 `region_base`，以及 `encode.cgi getConfigCaps` 能否解析出分辨率（解析不出时只提供当前分辨率）。

| 功能 | 大华 |
|---|---|
| 主/子码流预览 | ✅ 真实 NVR 验证 |
| 抓图 | ✅ 真机验证（`bc_client`） |
| 声音 | 🟡 AAC 帧能到达客户端（`bc_client`），但这台摄像头输出的音频几乎是静音（约 -91 dB），还没有确认过实际声音 |
| 移动、AI / 智能事件 | 🟡 已接，待真机验证（事件流已连通，只收到过 `NewFile`） |
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

## 快速开始

```bash
pip install -r requirements.txt        # cryptography + PyYAML；另需 ffmpeg/ffprobe
cp config.example.yaml config.yaml     # 填海康地址/账号/RTSP、虚拟摄像头的 IP 和密码
python -m bcproxy -c config.yaml -v
```

然后在 Reolink NVR 中手动添加摄像头：填 `listen` 中的 IP，端口 9000，用户名和密码与配置中的 `username` / `password` 一致。

**一台海康对应一个 IP**：NVR 按 IP 区分摄像头。Linux 上可以给代理主机加别名 IP（`ip addr add 192.168.1.201/24 dev eth0`），或用 Docker macvlan；Windows 的做法见下文。

## 不连 NVR 的本地验证

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

## 兼容性状态

在真实 Reolink NVR 和 Reolink 手机 App 上测试过（海康 2688×1520 H.265 主码流 + 640×360 子码流）：

| 功能 | 状态 | 说明 |
|---|---|---|
| 手动添加 / 登录 | ✅ 已验证 | FULL_AES（0x12）加密，回显 NVR 的包头 magic `0x0FEDCBA0` |
| 实时预览（主 / 子码流） | ✅ 已验证 | NVR 和 App 都能看 |
| 摄像头改名 | ✅ 已验证 | 保存到 `state_dir`，并同步修改海康通道名称（`hik.sync_name`） |
| AI / 智能侦测事件 | 🟡 代码完成 | msg 31/33；还需要在 NVR 上确认录像触发和图标显示 |
| 码流设置页（56 / 146） | 🟡 待验证 | v7 补了 `videoEncType`，码率改为读取海康的实际配置 |
| 码率 / CPU 占用 | 🟡 待 NVR 确认 | 122 回复 `<PerformanceInfo>`（`cpuUseRate`、`codeRate`、`netDataRate`），消息号和字段来自 Reolink Client 的 SDK；大华拿不到 CPU，显示 0 |
| App 设置页 | 🟡 待验证 | 37 端口、42 邮件、46 移动侦测、52 隐私遮挡、76 网络、112 码流、120 在线用户、132 图像，格式按真机抓包 |
| 码流分辨率 | 🟡 待 App 验证 | 146 列出海康提供的全部主码流分辨率（子码流和真机一样不可选）；57 写入后改海康的分辨率、帧率、码率并重连视频。App 发的 `videoEncType`、`encoderType` 不可信，不处理 |
| 录像设置页 | 🟡 待 App 验证 | 81/82 ↔ 海康录像计划（Normal=CMR，其余类型=EDR），54/55 ↔ 覆盖、预录、延录 |
| 高级网络设置页 | 🟡 待 App 验证 | 40/97/210/455，只保存在代理，不改海康网络 |
| Tamper Alert | 🟡 待 App 验证 | 199 `aitype` 第 25 位声明能力，763/764 读写海康遮挡侦测开关；海康 `shelteralarm` 上报为 `tamper` |
| 手动灯 | 🟡 待 App 验证 | 288 → 海康 supplementLight，`light_seconds`（默认 30 秒）后恢复原模式，291 推送状态 |
| 声音 | 🟡 待 App 验证 | 主码流音频转码为 16 kHz AAC，随预览发送（仅预览；海康无喇叭，不支持对讲） |
| Motion Boxes | ❌ 未实现 | 现有真机抓包中没有相关消息，需要抓开启该功能的真机 |
| 回放 | 🟡 待验证 | 海康 SD 卡：ISAPI 录像搜索 + RTSP 回放（只播所选的一段，播完停止）→ 102/142/14/15/16/381/382，拖动 383；海康无存储时自动关闭。298 时间轴缩略图未实现 |
| 局域网自动搜索 | 🟡 代码完成 | UDP 2000，回复格式按真机抓包；已确认 NVR 的搜索包能收到并回复，NVR“添加摄像头”列表中的显示还待确认 |
| 其他消息 | ❌ 未知 | 110、113、122 等回空 200；257 回 400、789/818 回 405（与真机一致） |

### 性能

实测（2.0 Mbps 主码流）：解析和封装加密约 0.16% 单核，ffmpeg 转发约 2% 单核，音频转码约 1.6% 单核。CPU 不是瓶颈，首帧等待（1.3～2 秒）主要花在 RTSP 和 ffmpeg 分析上。`-fflags nobuffer`、小 `-probesize` 在主码流和回放上出不了画面，不要用。

### ffmpeg 卡死

- **stderr 要一直读：** 不读的话缓冲区写满，ffmpeg 就阻塞在写 stderr 上，不再读摄像头，摄像头随后断开，而进程还活着。Crossroad 子码流会持续报 `non monotonically increasing dts`，用 concat 输入时约 8 分半就卡死，直连输入报得少，卡死得慢。
- **连接静默时 ffmpeg 不会退出：** 只有连接被关闭才会。所以 `STALL_SECONDS`（20 秒）内没有数据就重启 ffmpeg，并在 `errors.log` 记一条 `no data`。这条经常出现，说明摄像头或网络有问题。

### TODO：回放拖动

拖动进度条还有问题（App 上的具体表现待整理）。383 的回复格式是推断的，没有真机样本；真机上拖动时的抓包还没拿到。录像列表为了不超过单条消息上限会合并片段，拖到合并产生的空档会落到下一段真实录像。

### TODO：Motion Boxes

视频帧的扩展区里带框的数据（抓自 NVR 主码流，App 本地绘制，预览请求没有开关字段）：`ff <len16> { 01 0b00 { 01 0800 <u64 时间> } 02 <len16> { 01 0400 "lz4 " 02 <len16> <LZ4 帧> } }`。I 帧前面另有 8 字节时间。解压后是嵌套 TLV（`<u8 类型> <u16 长度> <值>`），网格 640×352（类型 3），每个框是类型 4：`x1 y1 x2 y2 score`（u16），可再带跟踪 ID 和类别。

还缺实时目标坐标：海康 RTSP 只有视频和音频。海康 App 的回放能显示框，所以数据应该和录像存在一起，下一步从这里查。App 的开关可能由 199 的 `thumbnail`、`ptzControl` 或 `newIspCfg` 第 6、7 位控制，未验证。

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
- **加密**：NVR 请求 FULL_AES（`0xdc12`），降级到 AES（0x02）会被直接断开。二进制载荷（视频、JPEG）整段加密，扩展头里带 `<encryptLen>`。真机还会带 `checkPos/checkValue`，并且只加密前 1024 字节；我们不这么做，NVR 和 App 也能正常解码。
- **局域网搜索**：NVR 每 2 秒从 UDP 3000 向 `255.255.255.255:2000` 广播 4 字节 `aa aa 00 00`，Reolink Client 也这样搜索。设备回复一个 388 字节的定长结构（IP、端口、名称、MAC、UID，字段偏移见 `bcproxy/discovery.py`）。回复**固定发到对方的 UDP 3000**，与请求的源端口无关。代理在 `0.0.0.0:2000` 上监听，每收到一次搜索，就为每台虚拟摄像头各回一个包。`tools/bc_discover.py` 可以列出局域网里的真机和虚拟摄像头。
- **设置消息**：NVR/App 写入的设置（25/45/57/105/209/297，以及“读 N、写 N+1”这种配对）会合并到对应的读取回复里，保存到 `state_dir/<camera>/<id>.xml`，删掉文件即可恢复默认。

## Windows 上运行

- `listen` 中的 IP 必须是本机网卡上已有的地址。要加别名 IP，网卡需要先改成静态 IP，再执行
  `netsh interface ipv4 add address "以太网" 192.168.0.201 255.255.255.0`。
- 9000 端口可能被 Hyper-V / WSL / Docker 保留（绑定报 `could not bind`）。用 `netsh interface ipv4 show excludedportrange protocol=tcp` 检查；如果被保留，就先 `net stop winnat`，再执行 `netsh int ipv4 add excludedportrange protocol=tcp startport=9000 numberofports=1`，最后 `net start winnat`。
- 防火墙放行入站 TCP 9000 和 UDP 2000（局域网搜索）：
  `New-NetFirewallRule -DisplayName "bcproxy 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow`
  `New-NetFirewallRule -DisplayName "bcproxy search" -Direction Inbound -Protocol UDP -LocalPort 2000 -Action Allow`
- 本机运行 Reolink Client 时，它会占用 UDP 3000，此时 `tools/bc_discover.py` 无法运行（代理本身不受影响）。
- 保存完整日志：`python -m bcproxy -c config.yaml -v *> log.txt`

## 部署到 UniFi Cloud Key Gen2 Plus

在 UCK-G2-Plus（UniFi OS 6.0.11，Debian 13，aarch64）上验证。全部文件都在 `/data/bcproxy`，开机由 systemd 启动。首次安装（Git Bash，在 `~/.ssh/config` 里配好 `UCK-G2-Plus`）：

```bash
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

## 用真机数据补全未适配的消息

1. 用真 Reolink 摄像头导出回复：
   `python tools/bc_client.py <真机IP> -p <密码> --dump-templates captured/ --get 26 46 56 76 110 112 113 122 132 146 208`
   加上 `--nvr-magic` 可以模拟 NVR 的包头。
2. 或者在 NVR 和真机之间抓包，然后解密：
   `python tools/bc_dump.py nvr.pcap -p <摄像头密码> --templates captured/`
   手机 App 不方便抓包时，可以用中继：`python tools/bc_relay.py <真机IP> --listen 0.0.0.0:9001 --out app.pcap`，然后在 App 里用 IP 添加“本机IP:9001”，再用 bc_dump.py 解密 `app.pcap`。
3. `captured/<id>.xml` 会优先于内置模板使用，型号、UID 等字段可以改成 `${model}`、`${uid}` 这样的占位符。
4. 代理开启 `trace: true` 时，日志里的 `unhandled msg N` 就是还缺模板的消息。每个连接里同一个 ID 只警告一次。

## 文件结构

```
bcproxy/
  crypto.py     BCEncrypt XOR / AES-128-CFB，登录哈希和密钥派生
  bc.py         Baichuan 帧编解码（头部、magic、扩展 XML、二进制模式、FULL_AES）
  bcmedia.py    BcMedia 容器（InfoV2 / I 帧 / P 帧 / AAC），Annex-B 访问单元切分
  session.py    服务端会话：登录协商、模板应答、设置写入、告警订阅(31/33)、预览(3/4)、抓图(109)
  camera.py     虚拟摄像头：海康事件 → 状态机 → 推送；能力位、改名、海康状态轮询
  discovery.py  局域网搜索应答（UDP 2000 → 3000）
  hikcfg.py     写到海康的设置：码流、录像计划、灯光
  dahua.py      大华：事件流、配置表、SD 卡录像（接口和 hik/hikcfg/HikRecordings 对应）
  playback.py   回放：录像搜索、事件标签、回放流
  settings.py   NVR/App 写入设置的合并与持久化
  events.py     事件状态机 + <AlarmEventList> 生成
  hik.py        ISAPI：Digest 认证、alertStream、multipart 解析、告警映射、HTTP 推送接收
  media.py      ffprobe/ffmpeg 拉 RTSP，多观看者共享一个 ffmpeg，码率统计
  logsetup.py   控制台和轮转日志文件，写入前去掉密码
  watchdog.py   systemd 就绪通知和看门狗心跳，检查后台任务是否还活着
  netalias.py   把摄像头的 listen IP 加到网卡上并保持
  templates/    内置回复模板（可被 templates_dir 覆盖）
deploy/         Cloud Key 部署：systemd 服务、设备上的安装脚本、PC 上的推送脚本
tools/
  bc_client.py  Baichuan 客户端（测试代理 / 从真机导出模板）
  bc_dump.py    pcap/pcapng 解密工具
  bc_discover.py 局域网搜索（列出真机和虚拟摄像头）
  bc_relay.py   App/NVR 与真机之间的录制中继（输出 pcap，交给 bc_dump.py 解密）
  fake_hik.py   假海康 ISAPI 服务
  fake_dahua.py 假大华 CGI 服务（含 PTZ）
tests/          协议测试（使用 neolink 的真机样本）+ 端到端测试：python -m pytest
```

## 其他限制

- 未实现：对讲、海康 PTZ、Motion Boxes、UDP/P2P 连接。回放只回放视频（无音频），播放速度固定 1 倍。
- 图像页（25/26/132）读写海康：亮度、对比度、饱和度、锐度、镜像翻转、电源频率、日夜模式、白平衡、3D 降噪。曝光、增益、背光、色调没有对接，保存后不生效。
- 服务器设置（37/38）读写海康：HTTPS、ONVIF 可改；禁用海康 HTTP/RTSP 或改它们的端口会被拒绝，因为代理依赖它们。
- 只保存在代理、不会写到海康的设置：移动侦测（47）、OSD、网络类页面（DHCP、DDNS、UPnP）。
- 回放的人/车/智能类型来自代理运行期间收到的告警（`state/<camera>/events.jsonl`）；海康录像索引本身不区分，所以更早的录像只显示移动。
- 时间戳使用代理收到帧的时间，不是摄像头 PTS。
- 智能侦测区域的几何信息（线、多边形坐标）没有模拟。如果 NVR 需要显示或编辑区域，请使用真机抓到的 527/529/531 模板。
- 26（图像参数）和 208（LED/红外灯）用的是参考 reolink_aio 写的默认模板，数值是固定的。

## 致谢

协议细节参考了 [neolink](https://github.com/QuantumEntangledAndy/neolink) 和 [reolink_aio](https://github.com/starkillerOG/reolink_aio)，`tests/samples/` 中的真机样本来自 neolink。
