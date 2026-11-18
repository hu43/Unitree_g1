#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G1 小野AI MCP 工具服务（stdio，MCP JSON-RPC 2.0）

职责：把"播放带 BGM 的舞蹈策略""蹲下/站起""限时移动"暴露成 MCP 工具。
不修改任何现有程序：
  - 舞蹈：只向 rt/wirelesscontroller 突发注入按键，由现有 g1_ctrl 处理，
          BGM 由现有 music_helper.py 随 rt/g1joystick/event 自动播放/停止。
  - 蹲/站/移动：只调用官方 LocoClient（sport 服务），不碰 g1_ctrl 与低层指令。
  - 绝不自动 Damp / ZeroTorque；移动一律限时并在退出路径兜底 StopMove。

运行环境：系统 python3.8 + G1_joystick/vendor（cyclonedds/unitree_sdk2py）。
  PYTHONPATH=/home/unitree/huang/unitree-app-templates/G1_joystick/vendor
  LD_LIBRARY_PATH=/home/unitree/huang/unitree-app-templates/G1_joystick/out/lib

--simulate 时不需要 DDS，用于本机协议自测。
stdout 只输出 MCP 报文，所有日志写 stderr。
"""

import argparse
import json
import os
import re
import signal
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
FSM_NAMES = {
    0: "零力矩",
    1: "阻尼",
    2: "蹲姿(旧码)",
    3: "坐姿",
    4: "预备",
    500: "启动",
    501: "行走",
    503: "舞蹈模式",
    702: "趴姿",
    706: "蹲姿",
    802: "走跑/站立",
    1000: "用户控制(策略执行中)",
}

FSM_RUN = 802
FSM_DANCE = 503
FSM_USER = 1000
FSM_DAMP = 1
FSM_ZERO = 0

KEY_BITS = {
    "R1": 0, "L1": 1, "Start": 2, "Select": 3,
    "R2": 4, "L2": 5, "F1": 6, "F2": 7,
    "A": 8, "B": 9, "X": 10, "Y": 11,
    "Up": 12, "Right": 13, "Down": 14, "Left": 15,
}

SUPPORTED_PROTOCOLS = ("2024-11-05", "2025-03-26", "2025-06-18")
PUBLISH_HZ = 100
CALL_GAP = 0.8          # 同一工具最小间隔，防语音重试连发
RATE_LIMIT_EXEMPT = ("robot_status", "dance_list")


def log(msg):
    print("[g1-mcp] %s" % msg, file=sys.stderr, flush=True)


def load_dances():
    with open(os.path.join(HERE, "dances.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def fsm_text(fsm):
    if fsm is None:
        return "未知"
    return "%s(%s)" % (fsm, FSM_NAMES.get(fsm, "自定义"))


def normalize_dance(text):
    """把语音可能给出的写法归一化成内部 key。"""
    t = (text or "").strip().lower()
    t = re.sub(r"[\s《》\"'“”‘’、,，.。!！?？]", "", t)
    return t


def process_cmdline_contains(substr):
    """只读扫描 /proc，判断某个命令是否在运行（不依赖 pgrep）。"""
    try:
        pids = os.listdir("/proc")
    except OSError:
        return False
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                raw = f.read().replace(b"\x00", b" ").decode("utf-8", "replace")
            if substr in raw and int(pid) != os.getpid():
                return True
        except (OSError, ValueError):
            continue
    return False


# ---------------------------------------------------------------------------
# 机器人驱动：DDS 按键注入 + 官方 LocoClient
# ---------------------------------------------------------------------------
class RobotDriver(object):
    def __init__(self, iface="eth0", simulate=False):
        self.iface = iface
        self.simulate = simulate
        self.loco = None
        self.channel = None
        self.cmd_pub = None
        self._string_cls = None
        self.event_sub = None
        self._event_lock = threading.Lock()
        self._events = []
        self.current_policy = None
        self._init_done = False
        self._sim_fsm = FSM_RUN
        self._sim_height = 0.5

    # ---- 初始化（懒加载：连不上云时也能先列出工具）----------------------
    def ensure_init(self):
        if self._init_done:
            return
        if self.simulate:
            log("simulate 模式：不初始化 DDS")
            self._init_done = True
            return

        from unitree_sdk2py.core.channel import (
            ChannelFactoryInitialize,
            ChannelFactory,
            ChannelPublisher,
            ChannelSubscriber,
        )
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import WirelessController_
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

        ChannelFactoryInitialize(0, self.iface)

        self.loco = LocoClient()
        self.loco.SetTimeout(5.0)
        self.loco.Init()

        factory = ChannelFactory()
        self.channel = factory.CreateChannel("rt/wirelesscontroller", WirelessController_)
        try:
            from cyclonedds.qos import Qos, Policy
            qos = Qos(Policy.Reliability.Reliable(100000000), Policy.History.KeepLast(10))
            self.channel.SetWriter(qos)
            log("rt/wirelesscontroller 使用 RELIABLE QoS")
        except Exception as e:  # 与 dance_chain 相同的回退策略
            log("RELIABLE QoS 设置失败，回退默认 BEST_EFFORT: %s" % e)
            try:
                self.channel.SetWriter(None)
            except Exception:
                pass

        # 语音指令通道（g1_ctrl 2026-11-17 补丁新增的只读主题）
        self._string_cls = String_
        self.cmd_pub = ChannelPublisher("rt/g1mcp/cmd", String_)
        self.cmd_pub.Init()

        # 事件回读：g1_ctrl 在进入/退出策略时发布 start:<状态> / stop:<状态>。
        # 带 music_lead 的策略会先播 4~12.5s 音乐再切用户控制，因此用事件确认
        # 比等 FSM=1000 更准确。
        self.event_sub = ChannelSubscriber("rt/g1joystick/event", String_)
        self.event_sub.Init(self._on_music_event)

        self._init_done = True
        log("DDS 初始化完成 (iface=%s)" % self.iface)

    def _on_music_event(self, msg):
        try:
            text = getattr(msg, "data", "") or ""
        except Exception:
            return
        self._emit_event(text)

    def _emit_event(self, text):
        with self._event_lock:
            self._events.append((time.time(), text))
            if len(self._events) > 50:
                self._events = self._events[-50:]
            if text.startswith("start:"):
                self.current_policy = text.split(":", 1)[1]
            elif text.startswith("stop:"):
                self.current_policy = None
        log("event: %s" % text)

    def wait_event(self, expected, timeout):
        """等待完全匹配的事件（例如 start:Su / stop:Su）。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._event_lock:
                for ts, text in self._events:
                    if text == expected and time.time() - ts < 20.0:
                        return True
            time.sleep(0.05)
        return False

    def wait_event_prefix(self, prefix, timeout):
        """等待指定前缀的事件，返回事件文本或 None。"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._event_lock:
                for ts, text in self._events:
                    if text.startswith(prefix) and time.time() - ts < 20.0:
                        return text
            time.sleep(0.05)
        return None

    # ---- 状态查询 -------------------------------------------------------
    def get_fsm(self):
        self.ensure_init()
        if self.simulate:
            return self._sim_fsm
        # vendor 里的 LocoClient 版本较老，没有 GetFsmId 便捷方法，直接走 7001
        try:
            code, data = self.loco._Call(7001, "{}")
            if code == 0 and data:
                val = json.loads(data).get("data")
                if isinstance(val, int):
                    return val
                log("GetFsmId 返回非整数: %r" % (val,))
            else:
                log("GetFsmId 失败 code=%s" % code)
        except Exception as e:
            log("GetFsmId 异常: %s" % e)
        return None

    def set_fsm(self, fsm_id):
        self.ensure_init()
        if self.simulate:
            # 模拟 706 的切换语义：站立<->蹲姿
            if int(fsm_id) == 706:
                self._sim_fsm = FSM_RUN if self._sim_fsm in (706, 2) else 706
            else:
                self._sim_fsm = int(fsm_id)
            return 0
        try:
            return self.loco.SetFsmId(int(fsm_id))
        except Exception as e:
            log("SetFsmId(%s) 异常: %s" % (fsm_id, e))
            return -1

    def get_stand_height(self):
        if self.simulate:
            return self._sim_height
        if self.loco is None:
            return None
        try:
            code, data = self.loco._Call(7005, "{}")
            if code == 0 and data:
                obj = json.loads(data)
                val = obj.get("data")
                if isinstance(val, (int, float)):
                    return float(val)
        except Exception as e:
            log("GetStandHeight 异常: %s" % e)
        return None

    def set_velocity(self, vx, vy, vyaw, duration):
        self.ensure_init()
        if self.simulate:
            log("simulate Move(%.2f,%.2f,%.2f,dur=%s)" % (vx, vy, vyaw, duration))
            return 0
        try:
            return self.loco.SetVelocity(float(vx), float(vy), float(vyaw), float(duration))
        except Exception as e:
            log("SetVelocity 异常: %s" % e)
            return -1

    def stop_move(self):
        return self.set_velocity(0.0, 0.0, 0.0, 1.0)

    # ---- 语音指令通道（g1_ctrl 新增）--------------------------------------
    def send_command(self, text):
        """向 rt/g1mcp/cmd 发布指令：状态名 或 "stop"。"""
        self.ensure_init()
        if self.simulate:
            log("simulate command: %s" % text)
            if text == "stop":
                cur = self.current_policy or "?"
                self._emit_event("stop:" + cur)
                self._sim_fsm = FSM_RUN
            else:
                self._emit_event("start:" + text)
                self._sim_fsm = FSM_USER
            return True
        try:
            self.cmd_pub.Write(self._string_cls(data=text))
            log("command -> rt/g1mcp/cmd: %s" % text)
            return True
        except Exception as e:
            log("指令发布失败: %s" % e)
            return False

    # ---- 按键突发注入（保留作兜底；当前固件已验证无效，不再使用）----------
    def burst_keys(self, modifier, trigger, hold=0.5):
        """先按住修饰键，再给触发键按下沿，保持 hold 秒后释放，最后归零。

        只在调用瞬间发布；不持续发零帧，避免遮蔽实体遥控器。
        """
        self.ensure_init()
        if self.simulate:
            log("simulate keys: %s + %s (hold=%.2fs)" % (modifier, trigger, hold))
            self._sim_fsm = FSM_USER
            time.sleep(0.02)
            return

        from unitree_sdk2py.idl.unitree_go.msg.dds_ import WirelessController_

        bit_mod = 1 << KEY_BITS[modifier]
        bit_trig = 1 << KEY_BITS[trigger]

        def send(keys, seconds):
            interval = 1.0 / PUBLISH_HZ
            end = time.time() + seconds
            while time.time() < end:
                self.channel.Write(WirelessController_(lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=keys))
                time.sleep(interval)

        send(bit_mod, 0.10)              # 修饰键先稳定
        send(bit_mod | bit_trig, hold)   # 触发键按下沿 + 保持
        send(bit_mod, 0.05)              # 释放触发键
        send(0, 0.08)                    # 清空，避免残留

    def double_click_start(self):
        """双击 Start：打断策略 / 取消官方舞蹈。"""
        self.ensure_init()
        if self.simulate:
            log("simulate double-click Start")
            self._sim_fsm = FSM_RUN
            time.sleep(0.05)
            return
        bit = 1 << KEY_BITS["Start"]

        def send(keys, seconds):
            interval = 1.0 / PUBLISH_HZ
            end = time.time() + seconds
            while time.time() < end:
                self.channel.Write(WirelessController_(lx=0.0, ly=0.0, rx=0.0, ry=0.0, keys=keys))
                time.sleep(interval)

        send(bit, 0.12)
        send(0, 0.15)
        send(bit, 0.12)
        send(0, 0.10)


# ---------------------------------------------------------------------------
# 工具实现
# ---------------------------------------------------------------------------
class ToolError(Exception):
    pass


class RobotTools(object):
    def __init__(self, driver, dances_cfg):
        self.d = driver
        self.cfg = dances_cfg
        self.dances = dances_cfg.get("dances", {})
        self.alias = dances_cfg.get("aliases", {})
        # 安全开关：默认全部关闭。2026-11-17 实机蹲姿发生摔倒后，
        # squat/stand_up/move 一律拒绝，禁止 MCP 发送 706/1/0/500/702。
        self.safety = dances_cfg.get("safety", {})
        self.allow_squat = bool(self.safety.get("allow_squat", False))
        self.allow_move = bool(self.safety.get("allow_move", False))
        self.hw_lock = threading.RLock()     # 串行化 DDS/RPC 调用
        self.move_evt = None
        self.move_thread = None
        self.moving = False
        self.squat_fsm = None                 # 探测后确认的蹲姿 FSM
        self.squat_mode = dances_cfg.get("squat_mode", "fsm706")
        self.last_call = {}
        self.squat_height = None

    # ---- 通用 -----------------------------------------------------------
    def _check_rate(self, name):
        if name in RATE_LIMIT_EXEMPT:
            return
        now = time.time()
        last = self.last_call.get(name, 0.0)
        if now - last < CALL_GAP:
            raise ToolError("操作太频繁，请稍候再试")
        self.last_call[name] = now

    def _guard_run(self, fsm, action):
        if fsm is None:
            raise ToolError("无法读取机器人状态（DDS/运控未就绪），请稍后再试")
        if fsm == FSM_USER:
            raise ToolError("机器人正在执行动作，请先停止或等它结束")
        if fsm == FSM_DANCE:
            raise ToolError("机器人处于舞蹈模式，请先停止当前舞蹈")
        if fsm == FSM_DAMP or fsm == FSM_ZERO:
            raise ToolError("机器人当前为%s，请先站起来" % FSM_NAMES.get(fsm))
        if fsm != FSM_RUN:
            raise ToolError("机器人当前为%s，不能%s；请先回到站立/走跑状态" % (fsm_text(fsm), action))

    def _wait_fsm(self, targets, timeout):
        t0 = time.time()
        while time.time() - t0 < timeout:
            cur = self.d.get_fsm()
            if cur in targets:
                return cur
            time.sleep(0.15)
        return None

    def stop_movement(self, quiet=True, force=False):
        """停止移动。默认只在确实移动时发 StopMove，避免无意义地干扰运动服务。"""
        if self.move_evt is not None:
            self.move_evt.set()
        if self.moving or force:
            with self.hw_lock:
                self.d.stop_move()
        self.moving = False
        if not quiet:
            log("已发送 StopMove")

    # ---- robot_status ---------------------------------------------------
    def robot_status(self, args):
        fsm = self.d.get_fsm()
        g1 = process_cmdline_contains("g1_ctrl") or self.d.simulate
        music = process_cmdline_contains("music_helper.py") or self.d.simulate
        pol = self.d.current_policy
        if fsm == FSM_USER:
            action = "策略执行中(%s)" % (pol or "?")
        elif pol:
            action = "策略准备中(%s，音乐前奏)" % pol
        elif fsm == FSM_DANCE:
            action = "官方舞蹈模式"
        elif self.moving:
            action = "移动中"
        else:
            action = "空闲"
        text = "状态：%s；动作：%s；g1_ctrl：%s；music_helper：%s" % (
            fsm_text(fsm), action,
            "运行" if g1 else "未运行",
            "运行" if music else "未运行",
        )
        return text, False

    # ---- dance_list -----------------------------------------------------
    def dance_list(self, args):
        lines = []
        for i, (key, info) in enumerate(sorted(self.dances.items()), 1):
            lines.append("%d. %s %ss BGM:%s" % (
                i, info.get("name", key), info.get("duration", "?"), info.get("bgm", "-")))
        return "\n".join(lines), False

    # ---- dance_play -----------------------------------------------------
    def resolve_dance(self, text):
        norm = normalize_dance(text)
        if norm in self.alias:
            return self.alias[norm]
        for key in self.dances:
            if norm == key.lower() or norm == normalize_dance(self.dances[key].get("name", "")):
                return key
        return None

    def dance_play(self, args):
        self._check_rate("dance_play")
        name = args.get("dance", "")
        key = self.resolve_dance(name)
        if key is None:
            raise ToolError("没有这个舞蹈：%s。可用：%s" % (
                name, "、".join(self.dances[k].get("name", k) for k in self.dances)))
        info = self.dances[key]

        if not process_cmdline_contains("g1_ctrl") and not self.d.simulate:
            raise ToolError("策略控制器 g1_ctrl 未运行，无法播放舞蹈")

        with self.hw_lock:
            fsm = self.d.get_fsm()
            if fsm == FSM_DANCE:
                raise ToolError("机器人处于官方舞蹈模式，请先用实体遥控器双击 Start 退出")
            if fsm == FSM_USER:
                raise ToolError("机器人正在执行动作，请先停止或等它结束")
            if fsm == FSM_DAMP or fsm == FSM_ZERO:
                raise ToolError("机器人当前为%s，请先站起来" % FSM_NAMES.get(fsm))
            if fsm != FSM_RUN:
                raise ToolError("机器人当前为%s，不能播放舞蹈；请先回到站立/走跑状态" % fsm_text(fsm))

            if not self.d.send_command(key):
                raise ToolError("指令发送失败，请检查 DDS 与 g1_ctrl 状态")
            entered = self.d.wait_event("start:" + key, 2.5)

        if not entered:
            cur = fsm_text(self.d.get_fsm())
            raise ToolError("已发送指令，但未收到启动事件（当前 %s）。请检查 g1_ctrl 是否加载了 rt/g1mcp/cmd 通道" % cur)
        return ("已开始《%s》，约%s秒，BGM《%s》同步。需要停止请说“停止舞蹈”。" % (
            info.get("name", key), info.get("duration", "?"), info.get("bgm", "-")), False)

    # ---- dance_stop -----------------------------------------------------
    def dance_stop(self, args):
        self._check_rate("dance_stop")
        self.stop_movement()
        with self.hw_lock:
            fsm = self.d.get_fsm()
            if fsm == FSM_DANCE:
                raise ToolError("机器人处于官方舞蹈模式，请用实体遥控器双击 Start 退出")
            if fsm != FSM_USER and self.d.current_policy is None:
                return "当前没有正在播放的舞蹈或动作（%s）" % fsm_text(fsm), False
            if not self.d.send_command("stop"):
                raise ToolError("停止指令发送失败，请用实体遥控器双击 Start")
            evt = self.d.wait_event_prefix("stop:", 8.0)
            back = self._wait_fsm((FSM_RUN,), 2.0)
        if evt is None and back is None:
            return ("停止请求已发送，但动作可能在音乐前奏结束后才停止，请稍后再查状态。", False)
        return "已停止当前动作，机器人回到站立状态。", False

    # ---- squat ----------------------------------------------------------
    def squat(self, args):
        self._check_rate("squat")
        # 2026-11-17 实机下蹲过程中摔倒，安全原因永久停用；
        # 即使 allow_squat=true 也只允许人工监护下由其他工具执行，MCP 不发送任何蹲姿指令。
        raise ToolError("出于安全原因，MCP 已停用蹲下功能，请使用实体遥控器操作。")

    # ---- stand_up -------------------------------------------------------
    def stand_up(self, args):
        self._check_rate("stand_up")
        # 停用：本固件起身需要先进入阻尼(1)，属于高风险状态切换（会失去支撑）。
        raise ToolError("出于安全原因，MCP 已停用起身功能，请使用实体遥控器操作。")

    # ---- move -----------------------------------------------------------
    def move(self, args):
        self._check_rate("move")
        # 移动属于实机动作，默认停用；需在人工监护下完成验证后才可开启。
        if not self.allow_move:
            raise ToolError("出于安全原因，MCP 移动控制已停用，请使用实体遥控器操作。")
        try:
            vx = float(args.get("vx", 0.0))
            vy = float(args.get("vy", 0.0))
            vyaw = float(args.get("vyaw", 0.0))
            duration = float(args.get("duration_s", 1.0))
        except (TypeError, ValueError):
            raise ToolError("参数必须是数字：vx, vy, vyaw, duration_s")

        vx = max(-0.5, min(0.5, vx))
        vy = max(-0.4, min(0.4, vy))
        vyaw = max(-0.8, min(0.8, vyaw))
        duration = max(0.2, min(5.0, duration))

        if duration > 3.0:
            raise ToolError("单次移动最多 3 秒，请分段执行")

        with self.hw_lock:
            fsm = self.d.get_fsm()
            if fsm != FSM_RUN:
                if fsm == FSM_USER:
                    raise ToolError("机器人正在执行动作，不能移动；请先停止")
                raise ToolError("机器人当前为%s，不能移动；请先站起来" % fsm_text(fsm))
            if self.moving:
                self.stop_movement()
            code = self.d.set_velocity(vx, vy, vyaw, 864000.0)  # 持续，由本地定时器兜底停
            if code != 0:
                raise ToolError("移动指令发送失败（code=%s）" % code)
            self.moving = True
            self.move_evt = threading.Event()
            self.move_thread = threading.Thread(
                target=self._move_watch, args=(duration, self.move_evt), daemon=True)
            self.move_thread.start()

        if abs(vx) >= abs(vy) and abs(vx) >= abs(vyaw):
            direction = "前进" if vx > 0 else "后退"
        elif abs(vy) >= abs(vyaw):
            direction = "左移" if vy > 0 else "右移"
        else:
            direction = "左转" if vyaw > 0 else "右转"
        return "已开始%s，%.1f 秒后自动停止。需要立即停请说“停止移动”。" % (direction, duration), False

    def _move_watch(self, duration, evt):
        t0 = time.time()
        try:
            while not evt.is_set():
                if time.time() - t0 >= duration:
                    break
                time.sleep(0.3)
                if not evt.is_set():
                    try:
                        if self.d.get_fsm() != FSM_RUN:
                            log("移动中 FSM 变化，提前停止")
                            break
                    except Exception:
                        break
        finally:
            try:
                with self.hw_lock:
                    self.d.stop_move()
            except Exception as e:
                log("StopMove 失败: %s" % e)
            self.moving = False
            log("移动结束（StopMove）")

    # ---- move_stop ------------------------------------------------------
    def move_stop(self, args):
        self._check_rate("move_stop")
        was = self.moving
        self.stop_movement()
        return ("已停止移动。" if was else "当前没有在移动。"), False


# ---------------------------------------------------------------------------
# MCP 工具声明（description 保持简短：设备端 tools/list 约 8KB 上限）
# ---------------------------------------------------------------------------
TOOL_SPECS = [
    {
        "name": "robot_status",
        "description": "查询机器人状态：FSM、g1_ctrl 与音乐进程、是否在执行动作。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dance_list",
        "description": "列出可播放的舞蹈、时长和 BGM 名称。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "dance_play",
        "description": "播放带 BGM 的舞蹈策略，立即返回。dance 取值：江南Style、机械舞1、机械舞2、Video27、功夫、广播体操、功夫3、Su。要求机器人站立。",
        "inputSchema": {
            "type": "object",
            "properties": {"dance": {"type": "string", "description": "舞蹈名称"}},
            "required": ["dance"],
            "additionalProperties": False,
        },
    },
    {
        "name": "dance_stop",
        "description": "立即停止当前舞蹈或策略（双击 Start），并回到站立。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "squat",
        "description": "（已因安全原因停用，请用实体遥控器）蹲下功能不再由 MCP 执行。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "stand_up",
        "description": "（已因安全原因停用，请用实体遥控器）起身功能不再由 MCP 执行。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "move",
        "description": "（已停用，请用实体遥控器）限时移动功能不再由 MCP 执行。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "vx": {"type": "number", "description": "前后速度，+前进"},
                "vy": {"type": "number", "description": "左右速度，+左移"},
                "vyaw": {"type": "number", "description": "转向速度，+左转"},
                "duration_s": {"type": "number", "description": "持续秒数，0.2-3"},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "move_stop",
        "description": "立即停止移动。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
]


# ---------------------------------------------------------------------------
# MCP JSON-RPC 服务
# ---------------------------------------------------------------------------
class McpServer(object):
    def __init__(self, tools):
        self.tools = tools
        self.handlers = {
            "robot_status": tools.robot_status,
            "dance_list": tools.dance_list,
            "dance_play": tools.dance_play,
            "dance_stop": tools.dance_stop,
            "squat": tools.squat,
            "stand_up": tools.stand_up,
            "move": tools.move,
            "move_stop": tools.move_stop,
        }
        self.specs = {s["name"]: s for s in TOOL_SPECS}
        self.write_lock = threading.Lock()
        self.initialized = False

    def send(self, obj):
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        with self.write_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def send_result(self, req_id, result):
        self.send({"jsonrpc": "2.0", "id": req_id, "result": result})

    def send_error(self, req_id, code, message):
        self.send({"jsonrpc": "2.0", "id": req_id,
                   "error": {"code": code, "message": message}})

    def handle(self, req):
        method = req.get("method")
        req_id = req.get("id")
        params = req.get("params") or {}

        if method == "initialize":
            pv = params.get("protocolVersion")
            reply_pv = pv if pv in SUPPORTED_PROTOCOLS else "2024-11-05"
            client = (params.get("clientInfo") or {}).get("name", "?")
            log("MCP initialize from %s (protocol=%s)" % (client, reply_pv))
            self.send_result(req_id, {
                "protocolVersion": reply_pv,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "g1-robot", "version": "1.0.0"},
            })
            return

        if method == "notifications/initialized":
            self.initialized = True
            log("MCP initialized")
            return

        if method == "ping":
            self.send_result(req_id, {})
            return

        if method == "tools/list":
            log("tools/list -> %d tools" % len(TOOL_SPECS))
            self.send_result(req_id, {"tools": TOOL_SPECS})
            return

        if method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            log("tools/call %s" % name)
            if name not in self.handlers:
                self.send_error(req_id, -32602, "Unknown tool: %s" % name)
                return
            try:
                text, is_error = self.handlers[name](args)
                self.send_result(req_id, {
                    "content": [{"type": "text", "text": text}],
                    "isError": bool(is_error),
                })
            except ToolError as e:
                self.send_result(req_id, {
                    "content": [{"type": "text", "text": str(e)}],
                    "isError": True,
                })
            except Exception as e:
                log("工具 %s 异常: %s" % (name, e))
                self.send_result(req_id, {
                    "content": [{"type": "text", "text": "内部错误：%s" % e}],
                    "isError": True,
                })
            return

        if req_id is None:
            return  # 未知通知，忽略
        self.send_error(req_id, -32601, "Method not found: %s" % method)

    def run(self):
        log("stdio MCP 服务已启动，工具：%s" % ", ".join(self.specs))
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                req = json.loads(raw)
            except Exception:
                self.send_error(None, -32700, "Parse error")
                continue
            # 每个请求独立线程，避免长动作阻塞 ping/status
            threading.Thread(target=self.handle, args=(req,), daemon=True).start()


# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="eth0", help="DDS 网卡，默认 eth0")
    ap.add_argument("--simulate", action="store_true", help="不连机器人，用于协议自测")
    args = ap.parse_args()

    cfg = load_dances()
    driver = RobotDriver(iface=args.iface, simulate=args.simulate)
    tools = RobotTools(driver, cfg)
    server = McpServer(tools)

    def _shutdown(signum, frame):
        log("收到信号 %s，准备退出" % signum)
        try:
            tools.stop_movement(force=True)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.run()


if __name__ == "__main__":
    main()
