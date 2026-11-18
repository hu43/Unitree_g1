# G1 小野AI MCP 工具（舞蹈 / 蹲起 / 移动）

把 G1 上**已训练好的 8 个带 BGM 舞蹈策略**和**限时移动**暴露给小野AI 语音助手。

> ## ⚠️ 安全状态（2026-11-17）
>
> 实机验证蹲姿时发生摔倒事故：`SetFsmId(706)` 进入蹲姿后再走阻尼(1)/706 的起身路径
> 在本机不稳定，会导致机器人失去支撑。因此：
>
> - **永久停用 `squat`、`stand_up`**：代码中不存在任何自动发送 `1/0/500/702/706` 的路径；
> - **`move` 仅在低速验证通过后开启**（当前 `safety.allow_move=true`，已用
>   0.1 m/s × 0.5 s 实机验证自动停）；保留 3 s 上限、限速与 FSM 离开 802 立即停；
> - **舞蹈**通过 `g1_ctrl` 新增的 `rt/g1mcp/cmd` 指令主题触发，BGM 仍由现有
>   `music_helper.py` 自动同步。
>
> 重新启用蹲/站前，必须在**机器人有支撑/保护、有人看护**的环境下重新设计并验证
> 一条**不使用阻尼**的起身路径。

本目录是**纯新增**。对现有 `g1_ctrl` 的改动是"只增不改"的 3 处（详见第五节），
原有遥控器按键逻辑一行未改。

## 一、工作原理

```
小野AI 云端  wss://api.xiaozhi.me/mcp/?token=<TOKEN>
      ▲  出站 WebSocket（token 只在 g1_mcp.env，600 权限）
  xz_pipe.py            # 桥：WebSocket <-> stdio，断线指数退避重连
      │  stdio 换行分隔 JSON-RPC 2.0
  g1_mcp_server.py      # 8 个 MCP 工具 + 安全互锁
      ├─ rt/g1mcp/cmd          → g1_ctrl（新增只增指令主题，状态名/stop）
      ├─ rt/g1joystick/event   ← 启动/停止事件回读（确认指令生效）
      └─ LocoClient (sport)    ← GetFsmId / SetVelocity（仅 move 用）
```

关键背景：**本机固件不支持虚拟遥控注入**。实测向 `rt/wirelesscontroller` 发布按键
可以匹配到订阅者，但 `rt/lowstate.wireless_remote` 始终为 0（4 秒 100Hz 注入 +
95Hz 采样确认），而 `g1_ctrl` 只读该字段。所以 `dance_chain` / `g1_virtual_remote`
假设的注入通道在这里不可用，改用新增的 `rt/g1mcp/cmd` 主题。

`g1_ctrl` 在以下两个内部状态都接受语音指令：
- `Passive`（开机/待机）
- `Exit`（一支舞蹈正常结束后会停在这里，原设计靠遥控键再触发）

带 `music_lead` 的策略（Gangnam/Video22/Video25/Video27/Jixiewu1/Jixiewu2 为 4s，
Kungfu3 为 12.5s）会先播音乐前奏、期间仍由官方控制；`dance_play` 用
`rt/g1joystick/event` 的 `start:<状态>` 事件确认，因此立即返回且不会误报失败。

## 二、工具（8 个）

| 工具 | 状态 | 说明 |
|---|---|---|
| `robot_status` | 可用 | FSM、g1_ctrl / music_helper、当前策略（前奏/执行中） |
| `dance_list` | 可用 | 8 个舞蹈的名称、时长、BGM |
| `dance_play(dance)` | 可用 | 立即返回；支持中文名与别名 |
| `dance_stop` | 可用 | 发 `stop`，走原 `exit_mimic` → Exit 路径（BGM 同步停止） |
| `squat` | **停用** | 只返回“请用实体遥控器”，不发送任何指令 |
| `stand_up` | **停用** | 只返回“请用实体遥控器”，不发送任何指令 |
| `move(...)` | 可用（低速验证后开启） | 限时 0.2–3 s、vx≤0.5/vy≤0.4/vyaw≤0.8，到时强制 StopMove |
| `move_stop` | 可用 | 仅在确实移动时发 StopMove |

## 三、可播放的 8 个策略

与 `G1_joystick/out/config/config.yaml` 的 transitions 完全一致（按键信息仅作对照，
MCP 走指令主题，不再注入按键）：

| 舞蹈 | 内部状态 | 遥控按键 | 时长 | BGM |
|---|---|---|---|---|
| 江南Style | Gangnam | F3+↑ | 20.4s | 江南Style（前奏 4s） |
| 机械舞1 | Jixiewu1 | F3+← | 20.6s | HOZIN 机械舞曲（前奏 4s） |
| 机械舞2 | Jixiewu2 | F3+→ | 12.4s | 霓虹迪斯科（前奏 4s） |
| Video27 | Video27 | F3+↓ | 28.2s | Wanderlust（前奏 4s） |
| 功夫 | Video22 | ↑+Y | 67.9s | 功夫曲（前奏 4s） |
| 广播体操 | Video25 | ↑+B | 91.6s | 功夫曲（前奏 4s） |
| 功夫3 | Kungfu3 | ↑+X | 92.8s | 男儿当自强（前奏 12.5s） |
| Su | Su | ←+X | 5.1s | SIUUU 音效（同起同停） |

## 四、部署

```bash
# 1) 拷贝目录到机器人
scp -r g1_mcp unitree@<robot-ip>:/home/unitree/huang/

# 2) 接入点（token 只落这个文件，600）
cat > /home/unitree/huang/g1_mcp/g1_mcp.env <<'EOF'
MCP_ENDPOINT='wss://api.xiaozhi.me/mcp/?token=<TOKEN>'
G1_MCP_IFACE=eth0
EOF
chmod 600 /home/unitree/huang/g1_mcp/g1_mcp.env

# 3) 手动前台试跑
cd /home/unitree/huang/g1_mcp && ./run.sh
```

依赖：机器人系统 `python3.8` 已自带 `websockets 11.0.3`，DDS 复用
`G1_joystick/vendor`，**无需安装任何包**。

### 开机自启（新增服务，不影响现有服务）

```bash
sudo cp /home/unitree/huang/g1_mcp/g1-mcp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now g1-mcp
systemctl status g1-mcp
journalctl -u g1-mcp -f
```

启动成功标志：

```
[xz-pipe] 已连接 wss://api.xiaozhi.me/mcp/
[g1-mcp] MCP initialize from xz-mcp-broker (protocol=2024-11-05)
[g1-mcp] tools/list -> 8 tools
```

## 五、对 g1_ctrl 的"只增不改"补丁

改动文件（全部为追加，不改变任何已有按键/状态逻辑）：

| 文件 | 改动 |
|---|---|
| `include/FSM/McpCommand.h` | **新增**：线程安全指令槽（start 3s 过期；stop 一次性标志） |
| `main.cpp` | **新增** 订阅 `rt/g1mcp/cmd`（std_msgs/String_），收到后写入指令槽 |
| `include/FSM/State_Passive.h` | **追加** 8 个状态名的检查；`exit()` 清指令 |
| `include/FSM/State_Exit.h` | **追加** 同样的检查（舞蹈结束停 Exit 时仍可语音触发） |
| `src/State_Mimic.cpp` | **追加** `stop` 检查，复用原 `Exit` 路径（会正常收尾 policy 线程并停 BGM） |

改动前已备份到 `/home/unitree/huang/G1_joystick_backup_20261117/`（源码 + `g1_ctrl.orig`）。

### 编译与安装（不删除原 out/，独立 build 目录）

```bash
cd /home/unitree/huang/unitree-app-templates/G1_joystick
cmake -S . -B build_mcp -DCMAKE_BUILD_TYPE=Release
nice -n 15 taskset -c 0-3 cmake --build build_mcp -j4   # 不抢 g1_ctrl 的 4-7 核
cp build_mcp/g1_ctrl out/bin/g1_ctrl.new && mv -f out/bin/g1_ctrl.new out/bin/g1_ctrl
sudo systemctl restart g1-joystick
```

重启后确认：

```bash
sudo journalctl -u g1-joystick -n 50 --no-pager | grep -E "\[mcp\]|FSM: Start"
# 期望： [mcp] listening on rt/g1mcp/cmd
```

### 回滚 g1_ctrl

```bash
B=/home/unitree/huang/G1_joystick_backup_20261117
J=/home/unitree/huang/unitree-app-templates/G1_joystick
sudo systemctl stop g1-joystick
cp "$B/g1_ctrl.orig" "$J/out/bin/g1_ctrl.new" && mv -f "$J/out/bin/g1_ctrl.new" "$J/out/bin/g1_ctrl"
cp -a "$B/main.cpp" "$J/main.cpp"
cp -a "$B/src/." "$J/src/"
cp -a "$B/include/." "$J/include/"
sudo systemctl start g1-joystick
```

## 六、验证

```bash
# 本地协议自测（不动机器人，含安全停用断言）
python3 test_mcp.py

# 实机只读状态（安全）
PYTHONPATH=/home/unitree/huang/unitree-app-templates/G1_joystick/vendor \
LD_LIBRARY_PATH=/home/unitree/huang/unitree-app-templates/G1_joystick/out/lib \
python3.8 probe_firmware.py --step status

# 舞蹈：最短的 Su
python3.8 test_live_dance.py --yes --dance Su

# 舞蹈 + 语音打断（12.4s 的机械舞2，3 秒后停）
python3.8 test_live_dance.py --yes --dance Jixiewu2 --stop-after 3

# 移动低速验证（约 5cm）
python3.8 test_live_move.py --yes --vx 0.1 --duration 0.5
```

> `probe_firmware.py --step squat/stand/move` 属于高风险动作探测，**默认禁止使用**；
> 仅在机器人有支撑/保护、有人看护时由负责人手动执行。

小野AI 控制台（https://xiaozhi.me/console/agents）→ 该智能体的 MCP 接入点 →
刷新连接状态/工具列表，应看到 8 个工具。

## 七、安全设计

1. **没有任何自动姿态切换**：`squat`/`stand_up` 永久停用，代码不存在对
   `1/0/500/702/706` 的自动调用。
2. **舞蹈只走指令主题**：`rt/g1mcp/cmd` → `g1_ctrl` 内部状态转移；动作仍由
   `g1_ctrl` 的安全检查（姿态异常 → Passive、超时 → Exit、DDS 断连 → Passive）兜底。
3. **动作前置校验**：仅官方 FSM=802 允许；1000/503/1/0 分别明确拒绝。
4. **停止复用原路径**：`stop` 触发与双击 Start 相同的 `Exit` 流程，BGM 必然同步停止。
5. **移动兜底**：限时 + 定时 StopMove + FSM 离开 802 立即停 + 进程退出兜底 StopMove。
6. 同工具 0.8 s 限流，防语音重试连发。
7. `Nice=5` + `taskset -c 0-3`，不抢 `g1_ctrl` 的 4–7 核。

## 八、已知约束

- **同一时间只执行一个动作**：`dance_play` 在 FSM=1000 时拒绝；`move` 在动作中拒绝。
- **带前奏的舞蹈**：`dance_play` 立即返回，`robot_status` 会显示
  “策略准备中(<名字>，音乐前奏)”；前奏期间机器人仍由官方控制。
- **停止延迟**：前奏期间发 `stop`，会在前奏结束后生效（最多 12.5s）；`dance_stop`
  会返回“停止请求已发送”而不是误报已停。
- **不要边用实体遥控器边语音操作**：两者都会驱动同一套状态机，可能互相抢占。
- **官方内置舞蹈（FSM 503）不支持语音**：请用实体遥控器；本方案只覆盖 8 个训练策略。
- `safety.allow_move` 若改回 `false`，`move` 会立即变为“已停用”，需重启服务生效。

## 九、回滚（整体）

```bash
sudo systemctl disable --now g1-mcp
sudo rm -f /etc/systemd/system/g1-mcp.service
sudo systemctl daemon-reload
rm -rf /home/unitree/huang/g1_mcp
# g1_ctrl 的改动按第五节"回滚 g1_ctrl"恢复
```

## 十、文件

| 文件 | 作用 |
|---|---|
| `g1_mcp_server.py` | stdio MCP 服务、工具实现、安全互锁、事件回读 |
| `xz_pipe.py` | 出站 WebSocket ↔ stdio 桥，重连与日志 |
| `dances.json` | 8 个策略的按键/时长/BGM/别名 + `safety` 开关 |
| `run.sh` | 加载 env、设置 PYTHONPATH/LD_LIBRARY_PATH、启动桥 |
| `g1-mcp.service` | 新增 systemd 单元 |
| `probe_firmware.py` | 固件 FSM 语义探测（默认只读；动作步骤高风险） |
| `test_mcp.py` | 本地协议/工具自测 |
| `test_live_dance.py` | 实机舞蹈验证（先检查 FSM=802，支持 `--stop-after`） |
| `test_live_move.py` | 实机移动低速验证 |
| `requirements.txt` | 仅 `websockets>=11`（系统已自带） |
| `g1_mcp.env` | 接入点地址与 token（600，不入库） |

## 十一、BGM 起播偏晚的修复（2026-11-18）

### 现象与定位

用户反馈“带前奏的舞蹈音乐偏晚 2 秒以上”。实测结论：

- `music_helper.py` 在收到 `start:<状态>` 事件后约 **50ms** 内就开始 `PlayStream`
  （日志与 C++ 事件同一秒，RPC 耗时 25ms），所以不是软件调度慢。
- 机器人麦克风是**回声消除（AEC）后的组播流**，会滤掉自身喇叭声，无法用麦克风
  直接测“实际出声”时刻。
- 对缓存 wav 做响度包络分析后发现：`Video22` / `Video25` 的**前 2.5 秒是轻柔前奏**
  （RMS ~320~450，正片 ~1300），`Video27` 前 1 秒完全静音。用户当时测试的正是
  `Video22`（日志里出现两次），与“只有带前奏的、晚 2 秒以上”完全吻合。

### 修复方式（保持动作与节拍对齐不变）

裁掉开头的轻柔/静音段，并等量缩短 `music_lead`，因此动作开始时刻对应的**歌曲位置**
不变，只是音乐提前到“一收到指令就出声”：

| 状态 | 裁掉 | 新缓存 | `music_lead` |
|---|---|---|---|
| Video22（功夫） | 2.5s | `Video22_2.5s.wav`（189.0s） | 4.0 → 1.5 |
| Video25（广播体操） | 2.5s | `Video25_2.5s.wav`（189.0s） | 4.0 → 1.5 |
| Video27 | 1.0s | `Video27_1.0s.wav`（167.4s） | 4.0 → 3.0 |

其余 5 个（江南Style、机械舞1/2、Kungfu3、Su）开头本来就出声，未改动。

### 改动文件与备份

- `music/music_helper.py`：`MUSIC` 里三处 `"start"`（0.0 → 2.5 / 2.5 / 1.0）
- `config/config.yaml` 与 `out/config/config.yaml`：三处 `music_lead`（两份保持同哈希）
- `music/cache/Video22_2.5s.wav`、`Video25_2.5s.wav`、`Video27_1.0s.wav`：**新增**
- `run.sh`：helper 改为 `python3.8 -u`（只让日志实时刷新，便于排查）

备份在 `/home/unitree/huang/G1_joystick_backup_20261117/`：
`music_helper.py.orig`、`config.yaml.src.orig`、`config.yaml.deployed.orig`、`run.sh.orig`。

### 回滚

```bash
B=/home/unitree/huang/G1_joystick_backup_20261117
J=/home/unitree/huang/unitree-app-templates/G1_joystick
cp -a "$B/music_helper.py.orig" "$J/music/music_helper.py"
cp -a "$B/config.yaml.src.orig" "$J/config/config.yaml"
cp -a "$B/config.yaml.deployed.orig" "$J/out/config/config.yaml"
cp -a "$B/run.sh.orig" "$J/run.sh"
sudo systemctl restart g1-joystick
```

### 验证日志

```
[music] event: start:Video22
[music] BGM lead-in 1.5s (music starts first)
[music] play Video22 (189.0s), volume=90     # 用的是裁剪后的缓存
FSM: Change state from Video22 to Exit       # 5s 后语音 stop 生效
```

### 11.4 事件丢失导致“偶发没音乐”的修复（2026-11-18）

后续发现语音触发后**偶发完全没有音乐**。排查结果：

- `music_helper` 早期用回调式订阅，实测单帧 BEST_EFFORT 事件 **5 次丢 2 次**，
  `stop` 事件 **5 次全部丢失**（MCP 自己的订阅者却能收到）。
- 修复：
  1. C++ `publish_music_event` 每个事件**连发 3 帧**（30ms 间隔）；
  2. `music_helper` 改为 **KEEP_LAST(20) 轮询读取**（`channel.Read(None)`），
     不再依赖回调线程；
  3. helper 对 **1 秒内同名事件去重**，避免连发 3 帧导致音乐被重启三次。
- 复测：
  - 单帧注入 5 轮：`play` 5/5、`stop` 5/5（修复前 3/5、0/5）；
  - 真实语音触发 Gangnam：`[music] play Gangnam` 恰好 1 次，
    `[music] stop (stop:Gangnam)` 恰好 1 次。

涉及文件：`src/State_Mimic.cpp`（连发 3 帧）与 `music/music_helper.py`（轮询 + 去重）。
`g1_ctrl` 需重新编译安装（见第五节）；本次二进制 sha256
`08ef178c55133184c48f3f999f9f4145e2d7bbcb7c76d0c3e26c211c59a5ed0d`。
