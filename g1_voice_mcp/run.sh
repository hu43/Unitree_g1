#!/bin/bash
# 启动 G1 小野AI MCP 桥（stdio 服务由 xz_pipe.py 拉起）
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
G1_JOYSTICK="${G1_JOYSTICK:-/home/unitree/huang/unitree-app-templates/G1_joystick}"

# 接入点地址（含 token）只放在 600 权限的 env 文件里
if [ -f "${HERE}/g1_mcp.env" ]; then
  set -a
  # shellcheck disable=SC1091
  . "${HERE}/g1_mcp.env"
  set +a
fi

# 复用 G1_joystick 已验证的 cyclonedds / unitree_sdk2py（不做任何安装）
export PYTHONPATH="${G1_JOYSTICK}/vendor${PYTHONPATH:+:${PYTHONPATH}}"
export LD_LIBRARY_PATH="${G1_JOYSTICK}/out/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

IFACE="${G1_MCP_IFACE:-eth0}"

exec taskset -c 0-3 nice -n 5 python3 "${HERE}/xz_pipe.py" --iface "${IFACE}"
