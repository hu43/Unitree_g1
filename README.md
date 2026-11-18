# Unitree G1 —— Web 控制台 + 语音 MCP

本分支 `feature/web-arm-control-voice-mcp` 在原有 DIY 指令卡项目之上，集成两块能力：

1. **`web_arm_control/`**：机器人 `http://<robot-ip>:5000/` 的完整 Web 控制台源码
   （机械臂、运动/FSM、导航建图、展览路径、人脸迎宾、手势控制、小夜语音代理等）。
2. **`g1_mcp/`**：小野AI 语音 MCP 工具（8 个），让小野能语音播放 8 个带 BGM 的
   训练策略、停止、限时移动。

另外附带 **`patches/`**：让语音真正能触发策略的控制器改动（新增 `rt/g1mcp/cmd`
指令主题），以及 BGM 前奏/丢帧修复。所有改动都是**只增不改**，原有实体遥控器逻辑未动。

> `main` 分支原有的 DIY 指令卡项目保持原样。原 README 见
> [README_DIY_CARD.md](README_DIY_CARD.md)。

## 目录结构

```
.
├── web_arm_control/          # 5000 端口 Web 控制台
│   ├── app.py                # Flask + SocketIO + DDS/ROS 主程序
│   ├── templates/index.html  # 单页前端
│   ├── xiaozhi_proxy.py      # 小夜/小智代理（token 存本地）
│   ├── announce_xiaoye.py
│   ├── start_service.sh      # 含 ROS 环境的启动脚本
│   ├── requirements.txt
│   └── xiaozhi_auth.example.json
├── g1_mcp/                   # 小野AI MCP 语音工具
│   ├── g1_mcp_server.py      # stdio MCP 服务（8 个工具 + 安全互锁）
│   ├── xz_pipe.py            # 出站 WebSocket ↔ stdio 桥
│   ├── dances.json           # 8 个策略的按键/时长/BGM/别名 + safety 开关
│   ├── run.sh / g1-mcp.service
│   ├── probe_firmware.py / test_mcp.py / test_live_dance.py
│   ├── test_live_move.py / test_event_reliability.py
│   ├── measure_audio.py / trim_bgm.py
│   ├── g1_mcp.env.example
│   └── README.md             # 详细部署、验证、回滚
├── patches/                  # 控制器与 BGM 补丁
│   ├── g1_ctrl/              # 新增 rt/g1mcp/cmd 指令主题
│   ├── g1_joystick/          # music_helper / config / run.sh
│   ├── g1_ctrl.patch
│   ├── g1_joystick.patch
│   └── README.md
├── README.md                 # 本文件
├── README_DIY_CARD.md        # main 分支原有项目说明（保留）
└── （main 分支原有 DIY 指令卡代码：g1_card_server.py、web/ 等）
```

## 一、web_arm_control（5000 端口控制台）

一个 Flask + SocketIO 单页应用，运行在机器人本机（Python 3.8），监听 `0.0.0.0:5000`。

### 功能分组

| 分组 | 功能 |
|---|---|
| 机械臂 | 16 种预设动作、动作列表、自定义执行（`/api/actions`、`/api/execute`） |
| 运动与模式 | FSM 模式切换、档位/速度、虚拟遥控、急停（`/api/set_fsm`、`/api/set_velocity`、`/api/emergency_stop`、`/api/set_gear`） |
| 状态与电量 | 健康检查、实时状态、BMS 电量（`/api/health`、`/api/status`） |
| 导航与建图 | 启动/停止导航、建图、保存/选择地图、PCD 校验、重定位、RViz 视频流（`/api/start_navigation`、`/api/start_mapping`、`/api/save_map`、`/api/relocate`、`/rviz_stream`） |
| 展览路径 | 站点增删改、保存/加载/运行路径（`/api/exhibition/*`） |
| 迎宾与手势 | 人脸迎宾、ROS2 手势控制（`/api/start_face_greet`、`/api/start_gesture_control`） |
| 小夜语音代理 | 启动/停止、token 管理、agent/音色/模型选择（`xiaozhi_proxy.py`、`/api/agent/*`） |
| 其他 | 网络接口枚举、语音动作列表、WebSocket 实时日志 |

### 启动

```bash
cd web_arm_control
pip3 install -r requirements.txt          # Flask 2.3.3 / Flask-SocketIO 5.3.5

export PYTHONPATH=/home/unitree/tang/WK/unitree_sdk2_python:$PYTHONPATH
export LD_LIBRARY_PATH=/home/unitree/tang/cyclonedds/install/lib:$LD_LIBRARY_PATH
python3.8 app.py eth0                     # http://<机器人IP>:5000

# 导航/手势等 ROS 子功能需要 ROS 环境，用项目自带脚本：
./start_service.sh
```

依赖：`unitree_sdk2py` + `cyclonedds`（机器人已装），ROS Noetic（仅导航/手势功能需要）。

### 安全与凭据

- 小夜/小智 token 只保存在机器人本地 `xiaozhi_auth.json`（**600 权限，不入库**），
  仓库只提供 `xiaozhi_auth.example.json`。
- `app.config['SECRET_KEY']` 仍是默认值 `secret!`，对外暴露前请改掉。
- 页面本身没有登录鉴权，请只在受控局域网内使用。

## 二、g1_mcp（小野AI 语音 MCP）

```
小野AI 云端  wss://api.xiaozhi.me/mcp/?token=<TOKEN>
      ▲  出站 WebSocket（token 只在 g1_mcp.env，600 权限）
  xz_pipe.py            # WebSocket ↔ stdio 桥，断线指数退避重连
      │  stdio / MCP JSON-RPC 2.0
  g1_mcp_server.py      # 8 个工具 + 安全互锁
      ├─ rt/g1mcp/cmd        → g1_ctrl（新增只增指令主题）
      ├─ rt/g1joystick/event ← 启动/停止事件回读（确认指令生效）
      └─ LocoClient (sport)  ← GetFsmId / SetVelocity（仅 move 用）
```

### 工具

| 工具 | 状态 | 说明 |
|---|---|---|
| `robot_status` | 可用 | FSM、g1_ctrl / music_helper、当前策略（前奏/执行中） |
| `dance_list` | 可用 | 8 个舞蹈的名称、时长、BGM |
| `dance_play(dance)` | 可用 | 立即返回；支持中文名与别名 |
| `dance_stop` | 可用 | 发 `stop`，走原 `exit_mimic` → Exit 路径，BGM 同步停止 |
| `squat` | **停用** | 实机蹲姿摔倒后永久停用，只提示用实体遥控器 |
| `stand_up` | **停用** | 同上，MCP 不发送任何姿态切换指令 |
| `move(vx,vy,vyaw,duration_s)` | 可用（限速） | 0.2–3 秒，到时强制 StopMove；仅 FSM=802 可用 |
| `move_stop` | 可用 | 仅在确实移动时发 StopMove |

### 可播放的 8 个策略

| 舞蹈 | 时长 | BGM |
|---|---|---|
| 江南Style | 20.4s | 江南Style（前奏 4s） |
| 机械舞1 | 20.6s | HOZIN 机械舞曲（前奏 4s） |
| 机械舞2 | 12.4s | 霓虹迪斯科（前奏 4s） |
| Video27 | 28.2s | Wanderlust（前奏 3s，已裁掉 1s 静音） |
| 功夫 | 67.9s | 功夫曲（前奏 1.5s，已裁掉 2.5s 轻柔段） |
| 广播体操 | 91.6s | 功夫曲（前奏 1.5s，已裁掉 2.5s 轻柔段） |
| 功夫3 | 92.8s | 男儿当自强（前奏 12.5s） |
| Su | 5.1s | SIUUU 音效（同起同停） |

### 部署

```bash
# 目录放到机器人
scp -r g1_mcp unitree@<robot-ip>:/home/unitree/huang/

# 接入点（600 权限，token 不入库）
cat > /home/unitree/huang/g1_mcp/g1_mcp.env <<'EOF'
MCP_ENDPOINT='wss://api.xiaozhi.me/mcp/?token=<TOKEN>'
G1_MCP_IFACE=eth0
EOF
chmod 600 /home/unitree/huang/g1_mcp/g1_mcp.env

# 手动试跑 / 开机自启
cd /home/unitree/huang/g1_mcp && ./run.sh
sudo cp g1-mcp.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now g1-mcp
```

依赖：机器人系统 `python3.8` 自带 `websockets 11`，DDS 复用现有 `G1_joystick/vendor`，
无需额外安装。

### 验证

```bash
python3 test_mcp.py                                   # 本地协议/工具自测
python3.8 probe_firmware.py --step status             # 实机只读
python3.8 test_live_dance.py --yes --dance Su         # 实机舞蹈（最短）
python3.8 test_live_dance.py --yes --dance Jixiewu2 --stop-after 3   # 中途停止
python3.8 test_live_move.py --yes --vx 0.1 --duration 0.5           # 移动低速
```

### 安全设计

- 只走 `rt/g1mcp/cmd` 指令主题，动作仍由 `g1_ctrl` 原有的姿态/超时/DDS 断连保护兜底。
- 仅官方 FSM=802 允许动作；1000/503/1/0 明确拒绝。
- 停止复用原 `Exit` 路径，BGM 必然同步停止。
- 移动保留 3 秒上限、速度限幅、到点强制 StopMove、FSM 离开 802 立即停、进程退出兜底。
- 不做任何自动 `Damp()` / `ZeroTorque`。

## 三、patches（控制器与 BGM 补丁）

### g1_ctrl：新增语音指令主题（只增不改）

| 文件 | 改动 |
|---|---|
| `include/FSM/McpCommand.h` | **新增**：线程安全指令槽（start 3s 过期；stop 一次性标志） |
| `main.cpp` | 新增订阅 `rt/g1mcp/cmd`（std_msgs/String_） |
| `include/FSM/State_Passive.h` | 追加 8 个状态名检查；`exit()` 清指令 |
| `include/FSM/State_Exit.h` | 追加相同检查（舞蹈结束后停在 Exit 时仍可语音触发） |
| `src/State_Mimic.cpp` | 追加 `stop` 检查；音乐事件连发 3 帧 |

为什么必须加：实测本固件**不支持虚拟遥控注入**（`rt/lowstate.wireless_remote` 恒为 0，
而 `g1_ctrl` 只读该字段），所以需要一条新的指令主题。

### g1_joystick：BGM 时序与可靠性

| 文件 | 改动 |
|---|---|
| `music/music_helper.py` | Video22/25 跳过 2.5s 轻柔前奏、Video27 跳过 1s 静音；订阅改为 KEEP_LAST(20) 轮询读取；1s 内同名事件去重 |
| `config/config.yaml` | 对应 `music_lead` 同步缩短（动作与节拍对齐不变） |
| `run.sh` | helper 加 `-u`（仅实时日志） |

为什么：实测单帧事件 5 次丢 2 次、`stop` 全丢（导致偶发没音乐）；Video22/25 开头偏轻、
Video27 开头静音（导致“音乐偏晚”）。

### 应用方式

见 [patches/README.md](patches/README.md)，核心步骤：

```bash
cd /home/unitree/huang/unitree-app-templates/G1_joystick
# 覆盖 g1_ctrl 文件后
cmake -S . -B build_mcp -DCMAKE_BUILD_TYPE=Release
nice -n 15 taskset -c 0-3 cmake --build build_mcp -j4
cp build_mcp/g1_ctrl out/bin/g1_ctrl.new && mv -f out/bin/g1_ctrl.new out/bin/g1_ctrl
sudo systemctl restart g1-joystick     # 成功标志：[mcp] listening on rt/g1mcp/cmd
```

裁剪后的 BGM 缓存（`Video22_2.5s.wav` 等）由 `g1_mcp/trim_bgm.py` 生成，不入库。

## 四、原有 DIY 指令卡（main 分支）

`main` 分支的 Scratch 风格指令卡项目（`g1_card_server.py`、`web/`、`workflows/` 等）
保持不变，详细说明见 [README_DIY_CARD.md](README_DIY_CARD.md)。

## 五、安全声明

本项目会驱动真实人形机器人运动：

- 首次运行前确认场地安全（2 米内无人、无障碍）、机器人站稳、急停/遥控器在手边。
- 蹲下/站起来已因实机摔倒事故停用，请使用实体遥控器。
- 语音触发会直接执行动作，请保持唤醒词唯一并避免在机器人负载不稳时使用。
- Web 控制台没有鉴权，请勿暴露到公网。

## 六、回滚

```bash
# 语音 MCP
sudo systemctl disable --now g1-mcp
rm -rf /home/unitree/huang/g1_mcp

# 控制器补丁：还原改动前备份后重启
sudo systemctl restart g1-joystick

# BGM：还原 music_helper.py / config.yaml / run.sh 备份后重启
```

详细命令见 `g1_mcp/README.md` 与 `patches/README.md`。
