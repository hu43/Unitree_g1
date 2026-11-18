#!/bin/bash
# ============================================================
# G1 遥控器多策略控制器 启动脚本（含 BGM 音乐伴随进程）
#
# 遥控器按键绑定（官方遥控器，2026-09-01 重排）：
#   F1 + Y -> Box        F1 + A -> Box1       F1 + X -> LeftPunch   F1 + B -> RightPunch
#   Up + Y -> Video22    Up + X -> Kungfu3    Up + B -> Video25 (Video21已解绑,不稳定)
#   Left + Y -> Bow      Left + X -> Su       Left + B -> Video28
#   F3 + Up -> Gangnam   F3 + Left -> Jixiewu1  F3 + Right -> Jixiewu2  F3 + Down -> Video27
#   BGM: Gangnam=江南Style / Video22=kungfu / Video25=kungfu(换曲) / Video27=Wanderlust
#        Kungfu3=男儿当自强(前奏12.5s播完开嗓才起动作) / Jixiewu1=HOZIN / Jixiewu2=霓虹迪斯科（先播4s）
#        Su=SIUUU音效（与动作同时起停）
#   双击 Start        -> 立即打断当前动作 -> Exit（音乐同步停止）
#
# 注意: 遥控器上第二个功能键(bit7) 网页端叫 F3、SDK 里叫 F2，同一物理键。
# 音乐: music/music_helper.py 订阅 rt/g1joystick/event，与动作同起同停；
#       python 依赖已打包进 vendor/，部署到其他 G1 无需 pip 安装。
# ============================================================
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# 音乐伴随进程（后台常驻，随本脚本退出一并结束）
# 注意: 必须用系统 python3.8 —— vendor/ 里的 cyclonedds 编译扩展是 cp38 的，
#       conda 环境的 python3 可能是其他版本，会报 No module named 'cyclonedds._clayer'
PY3="$(command -v python3.8 || command -v python3)"
PYTHONPATH="${SCRIPT_DIR}/vendor${PYTHONPATH:+:${PYTHONPATH}}" \
LD_LIBRARY_PATH="${SCRIPT_DIR}/out/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
"${PY3}" -u "${SCRIPT_DIR}/music/music_helper.py" eth0 &
HELPER_PID=$!
trap 'kill ${HELPER_PID} 2>/dev/null' EXIT

cd "${SCRIPT_DIR}/out/bin"
taskset -c 4-7 ./g1_ctrl
