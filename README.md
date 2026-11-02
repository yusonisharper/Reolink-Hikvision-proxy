# reolink-hik-proxy

把海康威视摄像头伪装成 Reolink 摄像头，让 Reolink NVR 用原生 Reolink 协议（Baichuan, TCP 9000）接入。重点是**事件映射**：海康的人形/车辆识别、越界、区域入侵、徘徊会被翻译成 Reolink 的 AI 和智能侦测事件，而不是 ONVIF 只能传的 motion。

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

- 海康 `regionID`（从 1 开始）映射为 Reolink 区域编号 `zone`（从 0 开始）。
- 越界这类单次脉冲告警会保持 `hold.smart` 秒，让 NVR 有时间触发录像；VMD 收到 `inactive` 时立即清除。
- NVR 是否认为摄像头支持这些功能，取决于 msg 199 `<Support>` 中的 `<aitype>`（bit1=人, bit2=车, bit4=猫狗）和 `<smartAI>`（bit1=越界, bit2=入侵, bit3=徘徊）。这两个值根据配置里的 `ai_types` 和 `smart` 自动生成；msg 527/529/531 的区域列表也由配置生成。

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

## 兼容性状态

在真实 Reolink NVR 和 Reolink 手机 App 上测试过（海康 2688×1520 H.265 主码流 + 640×360 子码流）：

| 功能 | 状态 | 说明 |
|---|---|---|
| 手动添加 / 登录 | ✅ 已验证 | FULL_AES（0x12）加密，回显 NVR 的包头 magic `0x0FEDCBA0` |
| 实时预览（主 / 子码流） | ✅ 已验证 | NVR 和 App 都能看 |
| 摄像头改名 | ✅ 已验证 | 保存到 `state_dir`，并同步修改海康通道名称（`hik.sync_name`） |
| AI / 智能侦测事件 | 🟡 代码完成 | msg 31/33；还需要在 NVR 上确认录像触发和图标显示 |
| 码流设置页（56 / 146） | 🟡 待验证 | v7 补了 `videoEncType`，码率改为读取海康的实际配置 |
| 码率 / CPU 占用 | ❌ 未适配 | 112、132 回复 `<Performance>` 无效，需要真机的 XML 结构 |
| 局域网自动搜索 | ❌ 未实现 | UDP 广播；可参考 neolink `discovery.rs` 实现 |
| 其他消息 | ❌ 未知 | 46、76、110、113、122、257、382、818 目前回空 200 |

### 协议上踩过的坑

- **包头 magic**：Reolink NVR 用 `0x0FEDCBA0`（`a0 cb ed 0f`），App 和 neolink 用 `0x0ABCDEF0`。摄像头必须用对端发来的那个 magic 回复，否则 NVR 会丢掉回复，约 200 ms 后断开，界面上显示为“连接超时”。
- **加密**：NVR 请求 FULL_AES（`0xdc12`），降级到 AES（0x02）会被直接断开。二进制载荷（视频、JPEG）整段加密，扩展头里带 `<encryptLen>`。真机还会带 `checkPos/checkValue`，并且只加密前 1024 字节；我们不这么做，NVR 和 App 也能正常解码。
- **设置消息**：NVR/App 写入的设置（25/45/57/105/209/297，以及“读 N、写 N+1”这种配对）会合并到对应的读取回复里，保存到 `state_dir/<camera>/<id>.xml`，删掉文件即可恢复默认。

## Windows 上运行

- `listen` 中的 IP 必须是本机网卡上已有的地址。要加别名 IP，网卡需要先改成静态 IP，再执行
  `netsh interface ipv4 add address "以太网" 192.168.0.201 255.255.255.0`。
- 9000 端口可能被 Hyper-V / WSL / Docker 保留（绑定报 `could not bind`）。用 `netsh interface ipv4 show excludedportrange protocol=tcp` 检查；如果被保留，就先 `net stop winnat`，再执行 `netsh int ipv4 add excludedportrange protocol=tcp startport=9000 numberofports=1`，最后 `net start winnat`。
- 防火墙放行入站 TCP 9000：
  `New-NetFirewallRule -DisplayName "bcproxy 9000" -Direction Inbound -Protocol TCP -LocalPort 9000 -Action Allow`
- 保存完整日志：`python -m bcproxy -c config.yaml -v *> log.txt`

## 用真机数据补全未适配的消息

1. 用真 Reolink 摄像头导出回复：
   `python tools/bc_client.py <真机IP> -p <密码> --dump-templates captured/ --get 26 46 56 76 110 112 113 122 132 146 208`
   加上 `--nvr-magic` 可以模拟 NVR 的包头。
2. 或者在 NVR 和真机之间抓包，然后解密：
   `python tools/bc_dump.py nvr.pcap -p <摄像头密码> --templates captured/`
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
  settings.py   NVR/App 写入设置的合并与持久化
  events.py     事件状态机 + <AlarmEventList> 生成
  hik.py        ISAPI：Digest 认证、alertStream、multipart 解析、告警映射、HTTP 推送接收
  media.py      ffprobe/ffmpeg 拉 RTSP，多观看者共享一个 ffmpeg，码率统计
  templates/    内置回复模板（可被 templates_dir 覆盖）
tools/
  bc_client.py  Baichuan 客户端（测试代理 / 从真机导出模板）
  bc_dump.py    pcap/pcapng 解密工具
  fake_hik.py   假海康 ISAPI 服务
tests/          协议测试（使用 neolink 的真机样本）+ 端到端测试：python -m pytest
```

## 其他限制

- 未实现：回放和录像检索（NVR 自己录像，不需要）、对讲、PTZ、音频转发、UDP/P2P 连接。
- 时间戳使用代理收到帧的时间，不是摄像头 PTS。
- 智能侦测区域的几何信息（线、多边形坐标）没有模拟。如果 NVR 需要显示或编辑区域，请使用真机抓到的 527/529/531 模板。
- 26（图像参数）和 208（LED/红外灯）用的是参考 reolink_aio 写的默认模板，数值是固定的。

## 致谢

协议细节参考了 [neolink](https://github.com/QuantumEntangledAndy/neolink) 和 [reolink_aio](https://github.com/starkillerOG/reolink_aio)，`tests/samples/` 中的真机样本来自 neolink。
