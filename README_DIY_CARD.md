# Unitree G1 DIY 指令卡

一张 Scratch 风格的可拼接机器人指令卡：拖拽积木拼出讲解/巡展流程，点击运行即可通过 Unitree SDK 在 G1 人形机器人上真实执行。

- 前端：单页 HTML，模块化积木、参数编辑、拖拽排序、俯视地图与手臂姿态演算。
- 后端：Python 标准库 HTTP 服务，通过 Unitree SDK2（DDS）下发运动、手臂动作和 TTS。
- 部署：systemd 常驻，开机自启，浏览器打开机器人 IP 即可使用。

![blocks](https://img.shields.io/badge/robot-G1-38bdf8) ![python](https://img.shields.io/badge/python-3.8-blue)

## 功能

| 功能 | 说明 |
|---|---|
| 积木拼接 | 前进/后退/左移/右移、左转/右转、右臂挥手、左臂挥手、向前挥手、放下手臂、复位、播报、停顿 |
| 真机执行 | 行走用 `SetVelocity` 持续下发；转向读取 `rt/odommodestate` 做闭环 |
| 语音播报 | `AudioClient.TtsMaker`，默认音量 100 |
| 保存工作流 | 保存到机器人磁盘 `workflows/`，可载入/删除 |
| 语音启动 | 订阅 `rt/audio_msg`，说“开始执行”或“启动工作流”即运行当前工作流 |
| 安全机制 | 现场确认、FSM 校验、限速、8 秒前端失联自动停车、停止即 `StopMove` + 手臂释放 |

## 目录结构

```
.
├── g1_card_server.py          # HTTP + DDS 后端
├── build_web.py               # 由 web_src/ + web_bridge.js 生成 web/index.html
├── web_src/
│   └── robot-diy-blocks.html  # 指令卡 UI（积木、舞台、演算）
├── web_bridge.js              # 真机桥接：调用后端 API、语音/工作流 UI
├── web/
│   ├── index.html             # 已生成的页面（可直接部署）
│   └── vendor/lucide-1.17.0.js
├── robot-diy-card.service     # systemd 单元
├── test_control_sim.py        # 闭环控制逻辑单元测试（模拟里程计）
├── test_features.py           # 保存工作流 / 语音触发 API 测试
└── workflows/                 # 运行时生成的工作流 JSON
```

## 环境要求

- 机器人：Unitree G1，内置运控可用（走跑运控 FSM 802，或旧固件 801）。
- 机器人侧：Python 3.8、`unitree_sdk2py` 已安装、`eth0` 在机器人内网（192.168.123.x）。
- 浏览器与机器人同一局域网。

## 部署

```bash
# 在机器人上
mkdir -p /home/unitree/robot-diy-card
# 将本仓库的 g1_card_server.py、web/、web_bridge.js 复制到该目录

sudo cp robot-diy-card.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now robot-diy-card

# 查看状态
systemctl status robot-diy-card
curl http://127.0.0.1:8080/api/status
```

浏览器访问：`http://<机器人IP>:8080/`

## 使用

1. 站在机器人旁边，确认 2 米内无人、无障碍，遥控器在手边。
2. 拖拽左侧积木到中间拼流程，参数可直接改。
3. 点“运行”，在确认弹窗点“确定”。
4. 停止：点“停止”或按遥控器急停；关闭页面 8 秒后后端会自动停车。

### 积木 → 真机接口

| 积木 | 执行方式 |
|---|---|
| 向前/后、左/右移 | `LocoClient.SetVelocity`，按里程计位移闭环停车 |
| 左转/右转 | `SetVelocity(0,0,±1.0)` 持续下发，按 yaw 闭环停车 |
| 右臂挥手 | 手臂动作 23 `right_hand_up` |
| 向前挥手 | 手臂动作 25 `wave_under_head` |
| 左臂挥手 | 固件无独立左臂挥手，使用 15 `both_hands_up` |
| 放下手臂 / 复位 | 手臂动作 99 `release_arm` |
| 播报 | `AudioClient.TtsMaker(text, 0)` |
| 停顿 | 阻塞等待 |

### 语音启动

- 机器人需处于唤醒模式（APP/遥控器切换）。
- 说 **“开始执行”** 或 **“启动工作流”**。
- 运行的是当前页面同步给后端的工作流；页面关闭后使用最后一次同步的内容。
- 10 秒冷却；FSM 不满足或机器人未站稳时拒绝执行。
- 关键词可通过环境变量 `G1_VOICE_KEYWORDS` 修改，语音开关在页面上。

### 保存工作流

- “保存工作流”输入名称，写入机器人 `workflows/<名称>.json`。
- 下拉框选择即可载入；“删除”会二次确认。
- 页面上的当前积木会每 1.2 秒自动同步给后端，供语音启动使用。

## 配置（systemd 环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `G1_IFACE` | `eth0` | DDS 网卡 |
| `G1_PORT` | `8080` | HTTP 端口 |
| `G1_MAX_SPEED` | `0.6` | 最大步速 m/s |
| `G1_VOLUME` | `100` | TTS 音量 0–100 |
| `G1_TURN_RATE` | `1.0` | 转向角速度 rad/s（重要，见下） |
| `G1_VY_LEFT_SIGN` | `1` | 左移方向，若左右相反改为 `-1` |
| `G1_WATCHDOG` | `8` | 前端失联自动停车秒数，0 关闭 |
| `G1_VOICE_ENABLE` | `1` | 语音启动开关 |
| `G1_VOICE_KEYWORDS` | `开始执行,启动工作流` | 触发词 |
| `G1_DRY_RUN` | `0` | 1 = 只演算不下发 |

## 已知行为

- **转向死区**：该固件在 FSM 802 下纯 yaw 指令 0.5 rad/s 会被忽略，需使用 1.0 rad/s；本项目已按 1.0 配置，并保留闭环停车与方向自动纠正。
- 原地转向时步态会让机器人位置漂移几厘米到十几厘米；地图不显示这部分漂移，后续位移仍按里程计闭环。
- 距离控制依赖里程计；地面打滑或搬运机器人后需要重新对齐。
- `is_final` 在默认非流式 ASR 下可能始终为 false，因此语音触发使用“关键词 + 置信度 + 去重”，不依赖该字段。

## 测试

```bash
# 闭环控制逻辑（无需机器人）
python3 test_control_sim.py

# 工作流/语音 API（机器人后端已启动）
python3 test_features.py
```

## 开发

修改 `web_src/robot-diy-blocks.html` 或 `web_bridge.js` 后重新生成页面：

```bash
python3 build_web.py
```

后端接口：

```
GET  /api/status              机器人/SDK/FSM/音量/里程计/ASR 状态
GET  /api/state               当前任务进度、位姿、日志
POST /api/run                 执行工作流 {"confirm":true,"speed":0.5,"blocks":[...]}
POST /api/stop                停止（StopMove + 手臂释放）
GET/POST/DELETE /api/workflows  工作流保存/列表/载入/删除
POST /api/current             同步当前工作流（语音启动使用）
GET/POST /api/voice           语音开关与最近识别结果
POST /api/voice/trigger       测试语音启动（不经过麦克风）
```

## 安全声明

本项目会驱动真实人形机器人运动。首次运行前必须确认场地安全、机器人站稳、急停手段可用。语音启动会直接执行当前工作流，请谨慎选择工作流内容并保持唤醒词唯一。
