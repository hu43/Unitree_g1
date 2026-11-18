# 控制器与 BGM 补丁（只增不改）

本目录是对现有 `G1_joystick` 的改动，全部为**追加**；原有实体遥控器按键逻辑一行未改。
完整 diff 见 `g1_ctrl.patch` / `g1_joystick.patch`（基于 2026-09 的原始版本）。

## g1_ctrl/（语音指令通道）

| 文件 | 改动 |
|---|---|
| `include/FSM/McpCommand.h` | **新增**：线程安全指令槽（start 3s 过期；stop 一次性标志） |
| `main.cpp` | 新增订阅 `rt/g1mcp/cmd`（std_msgs/String_），写入指令槽 |
| `include/FSM/State_Passive.h` | 追加 8 个状态名检查；`exit()` 清指令 |
| `include/FSM/State_Exit.h` | 追加相同检查（舞蹈结束停 Exit 时仍可语音触发） |
| `src/State_Mimic.cpp` | 追加 `stop` 检查（复用原 Exit 路径）；音乐事件连发 3 帧 |

为什么需要：该固件**不支持虚拟遥控注入**（实测 `rt/lowstate.wireless_remote` 恒为 0），
`g1_ctrl` 只读该字段，因此必须新增一条指令主题。

应用与编译：

```bash
J=/home/unitree/huang/unitree-app-templates/G1_joystick
cp -a patches/g1_ctrl/main.cpp            "$J/main.cpp"
cp -a patches/g1_ctrl/src/State_Mimic.cpp "$J/src/State_Mimic.cpp"
cp -a patches/g1_ctrl/include/FSM/*.h     "$J/include/FSM/"
cd "$J"
cmake -S . -B build_mcp -DCMAKE_BUILD_TYPE=Release
nice -n 15 taskset -c 0-3 cmake --build build_mcp -j4
cp build_mcp/g1_ctrl out/bin/g1_ctrl.new && mv -f out/bin/g1_ctrl.new out/bin/g1_ctrl
sudo systemctl restart g1-joystick
# 成功标志：journal 出现 "[mcp] listening on rt/g1mcp/cmd"
```

回滚：还原改动前的 `main.cpp` / `src/` / `include/` 与 `out/bin/g1_ctrl` 备份后重启服务。

## g1_joystick/（BGM 时序与可靠性）

| 文件 | 改动 |
|---|---|
| `music/music_helper.py` | 三处歌曲跳过轻柔前奏（Video22/25 2.5s、Video27 1.0s）；订阅改为 KEEP_LAST(20) 轮询读取；1s 内同名事件去重 |
| `config/config.yaml` | 对应 `music_lead` 同步缩短（4.0→1.5 / 1.5 / 3.0），动作与节拍对齐不变 |
| `run.sh` | helper 启动加 `-u`（仅日志实时刷新） |

为什么：实测单帧 BEST_EFFORT 事件 5 次丢 2 次、stop 全丢（导致偶发没音乐）；
Video22/25 开头 2.5s 很轻、Video27 开头 1s 静音（导致“音乐偏晚”）。

`Video22_2.5s.wav` 等裁剪缓存由 `g1_mcp/trim_bgm.py` 生成，不入库。
