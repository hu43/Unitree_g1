#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G1 DIY command card backend.
Serves the card UI and executes workflow blocks on a Unitree G1.

Features:
  - closed-loop walking / turning using rt/odommodestate
  - arm actions via the arm action service
  - TTS voice announcements, volume set to G1_VOLUME
  - saved workflows on disk
  - voice start via rt/audio_msg ASR keyword

Endpoints:
  GET  /api/status
  GET  /api/state
  GET  /api/actions
  GET  /api/voice
  POST /api/voice          {"enabled": true}
  POST /api/voice/trigger  test hook, runs the current workflow
  GET  /api/workflows
  POST /api/workflows      {"name": "...", "blocks": [...], "speed": 0.5}
  GET  /api/workflows/get?name=...
  DELETE /api/workflows?name=...
  POST /api/current        {"blocks": [...], "speed": 0.5, "name": "..."}
  POST /api/run            {"confirm": true, "speed": 0.5, "blocks": [...]}
  POST /api/stop
"""

import json
import math
import os
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(BASE_DIR, "web")
WORKFLOW_DIR = os.path.join(BASE_DIR, "workflows")
CURRENT_FILE = os.path.join(WORKFLOW_DIR, "_current.json")

IFACE = os.environ.get("G1_IFACE", "eth0")
PORT = int(os.environ.get("G1_PORT", "8080"))
DRY_RUN = os.environ.get("G1_DRY_RUN", "0") == "1"
MAX_SPEED = float(os.environ.get("G1_MAX_SPEED", "0.6"))
MAX_DISTANCE = float(os.environ.get("G1_MAX_DISTANCE", "5.0"))
MAX_TURN_DEG = float(os.environ.get("G1_MAX_TURN_DEG", "180"))
MAX_BLOCKS = int(os.environ.get("G1_MAX_BLOCKS", "60"))
MAX_TEXT = int(os.environ.get("G1_MAX_TEXT", "200"))
SPEAKER_ID = int(os.environ.get("G1_SPEAKER", "0"))
VOLUME = max(0, min(100, int(os.environ.get("G1_VOLUME", "100"))))
VY_LEFT_SIGN = 1.0 if os.environ.get("G1_VY_LEFT_SIGN", "1") != "-1" else -1.0
TURN_RATE = float(os.environ.get("G1_TURN_RATE", "1.0"))  # rad/s
STREAM_INTERVAL = 0.12
STREAM_DURATION = 0.5
ODOM_MAX_AGE = 1.0
WATCHDOG = float(os.environ.get("G1_WATCHDOG", "8"))
MAX_TASK_SECONDS = float(os.environ.get("G1_MAX_TASK_SECONDS", "300"))
VOICE_ENABLE_DEFAULT = os.environ.get("G1_VOICE_ENABLE", "1") != "0"
VOICE_KEYWORDS = [k.strip() for k in
                  os.environ.get("G1_VOICE_KEYWORDS",
                                 "开始执行,启动工作流").split(",") if k.strip()]
VOICE_COOLDOWN = float(os.environ.get("G1_VOICE_COOLDOWN", "10"))
ASR_MIN_CONF = float(os.environ.get("G1_ASR_MIN_CONF", "0.5"))

WALK_FSM = (801, 802)  # 801 legacy, 802 = walk-run control (ai_sport >= 8.6)

LOG_LIMIT = 120

state_lock = threading.Lock()
sdk_lock = threading.Lock()
odom_lock = threading.Lock()
asr_lock = threading.Lock()
cancel_evt = threading.Event()

loco = None
arm = None
audio = None
_odom_sub = None
_asr_sub = None
_factory_ready = False
_last_init_attempt = 0.0

sdk = {
    "ok": False,
    "dry_run": DRY_RUN,
    "error": None,
    "fsm_id": None,
    "fsm_mode": None,
    "volume": None,
    "actions": None,
    "odom": False,
    "asr": False,
    "checked_at": 0.0,
}

odom = {"ok": False, "x": 0.0, "y": 0.0, "yaw": 0.0, "t": 0.0}

asr = {
    "last_text": "",
    "last_final": False,
    "last_t": 0.0,
    "last_confidence": 0.0,
    "trigger_count": 0,
    "last_trigger_t": 0.0,
    "last_trigger_text": "",
}

voice = {"enabled": VOICE_ENABLE_DEFAULT}

voice_workflow = {"blocks": [], "speed": 0.5, "name": ""}

state = {
    "running": False,
    "task_id": 0,
    "index": -1,
    "total": 0,
    "progress": 0.0,
    "block_progress": 0.0,
    "now": "就绪",
    "status": "待命",
    "error": None,
    "speech": None,
    "arm_mode": "down",
    "pose": {"x": 0.0, "y": 0.0, "heading": 0.0},
    "log": [],
    "started_at": None,
    "finished_at": None,
    "last_poll": 0.0,
    "source": "",
}


def log(text, code=None):
    line = time.strftime("%H:%M:%S") + "  " + str(text)
    if code is not None:
        line += "  (code=%s)" % code
    print(line, flush=True)
    with state_lock:
        state["log"].append(line)
        if len(state["log"]) > LOG_LIMIT:
            del state["log"][0:len(state["log"]) - LOG_LIMIT]


def set_state(**kw):
    with state_lock:
        state.update(kw)


def set_progress(index, total, local):
    total = max(1, total)
    p = (index + max(0.0, min(1.0, local))) / total
    with state_lock:
        state["progress"] = max(0.0, min(1.0, p))
        state["block_progress"] = max(0.0, min(1.0, local))


def _set_timeout(client):
    try:
        client.SetTimeout(5.0)
    except Exception:
        pass


def _odom_handler(msg):
    try:
        rpy = msg.imu_state.rpy
        with odom_lock:
            odom["x"] = float(msg.position[0])
            odom["y"] = float(msg.position[1])
            odom["yaw"] = float(rpy[2])
            odom["t"] = time.time()
            odom["ok"] = True
    except Exception:
        pass


def _asr_handler(msg):
    try:
        data = json.loads(msg.data)
    except Exception:
        return
    text = str(data.get("text") or "").strip()
    try:
        conf = float(data.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    final = bool(data.get("is_final"))
    now = time.time()
    with asr_lock:
        prev_text = asr["last_text"]
        prev_t = asr["last_t"]
        asr["last_text"] = text
        asr["last_final"] = final
        asr["last_t"] = now
        asr["last_confidence"] = conf
    if not voice["enabled"] or not text or conf < ASR_MIN_CONF:
        return
    # 默认是非流式 ASR，is_final 可能始终为 false，因此不能依赖它。
    cleaned = text.strip("。！？!?，, .")
    if not any(k in cleaned for k in VOICE_KEYWORDS):
        return
    if text == prev_text and now - prev_t < 4.0:
        return
    log("ASR 命中：%s（conf=%.2f, final=%s）" % (text, conf, final))
    threading.Thread(target=_voice_trigger, args=(text,), daemon=True).start()


def _voice_trigger(text):
    now = time.time()
    with state_lock:
        running = state["running"]
        last = asr["last_trigger_t"]
    if running:
        log("语音识别到「%s」，但已有任务在执行" % text)
        return
    if now - last < VOICE_COOLDOWN:
        log("语音识别到「%s」，冷却中（%.0fs）" % (text, VOICE_COOLDOWN))
        return
    with state_lock:
        blocks = list(voice_workflow["blocks"])
        speed = voice_workflow["speed"]
        name = voice_workflow["name"]
    if not blocks:
        log("语音识别到「%s」，但当前没有同步工作流，已忽略" % text)
        return
    log("语音启动：「%s」→ 工作流 %s（%d 条）" % (text, name or "(未命名)", len(blocks)))
    ok, err, tid, _code = start_task(blocks, speed, source="voice")
    if ok:
        with asr_lock:
            asr["trigger_count"] += 1
            asr["last_trigger_t"] = now
            asr["last_trigger_text"] = text
    else:
        log("语音启动失败：%s" % err)


def init_sdk():
    global loco, arm, audio, sdk, _factory_ready, _odom_sub, _asr_sub
    if DRY_RUN:
        sdk.update(ok=True, dry_run=True, error=None, fsm_id=802, fsm_mode=0,
                   volume=VOLUME, checked_at=time.time())
        log("DRY-RUN 模式：不连接机器人，仅演算指令")
        return
    try:
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelSubscriber
        from unitree_sdk2py.idl.unitree_go.msg.dds_ import SportModeState_
        from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

        if not _factory_ready:
            ChannelFactoryInitialize(0, IFACE)
            _factory_ready = True

        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        from unitree_sdk2py.g1.arm.g1_arm_action_client import G1ArmActionClient
        from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient

        loco = LocoClient()
        _set_timeout(loco)
        loco.Init()

        arm = G1ArmActionClient()
        _set_timeout(arm)
        arm.Init()

        audio = AudioClient()
        _set_timeout(audio)
        audio.Init()
        try:
            audio.SetVolume(VOLUME)
            code, v = audio.GetVolume()
            if code == 0 and isinstance(v, dict):
                sdk["volume"] = v.get("volume")
            log("音量设置为 %d（当前 %s）" % (VOLUME, sdk["volume"]))
        except Exception as e:
            log("设置音量失败: %r" % (e,))

        if _odom_sub is None:
            _odom_sub = ChannelSubscriber("rt/odommodestate", SportModeState_)
            _odom_sub.Init(_odom_handler, 10)
        if _asr_sub is None:
            _asr_sub = ChannelSubscriber("rt/audio_msg", String_)
            _asr_sub.Init(_asr_handler, 10)

        sdk["ok"] = True
        sdk["error"] = None
        sdk["odom"] = True
        sdk["asr"] = True
        log("SDK 初始化成功，接口 %s" % IFACE)
        refresh_status(force=True)
        try:
            code, data = arm.GetActionList()
            if code == 0:
                sdk["actions"] = data
                log("已读取手臂动作列表")
        except Exception as e:
            log("读取动作列表失败: %r" % (e,))
    except Exception as e:
        sdk["ok"] = False
        sdk["error"] = repr(e)
        log("SDK 初始化失败: %r" % (e,))
        log(traceback.format_exc())


def try_init():
    global _last_init_attempt
    if DRY_RUN or sdk["ok"]:
        return sdk["ok"]
    if time.time() - _last_init_attempt < 8:
        return False
    _last_init_attempt = time.time()
    log("尝试重新连接机器人 SDK…")
    init_sdk()
    return sdk["ok"]


def refresh_status(force=False):
    if DRY_RUN:
        return sdk
    with sdk_lock:
        if not force and time.time() - sdk["checked_at"] < 1.5:
            return sdk
        try:
            code, data = loco._Call(7001, json.dumps({}))
            if code == 0:
                sdk["fsm_id"] = json.loads(data).get("data")
            code2, data2 = loco._Call(7002, json.dumps({}))
            if code2 == 0:
                sdk["fsm_mode"] = json.loads(data2).get("data")
            if audio is not None:
                vcode, vdata = audio.GetVolume()
                if vcode == 0 and isinstance(vdata, dict):
                    sdk["volume"] = vdata.get("volume")
            with odom_lock:
                sdk["odom"] = odom["ok"]
            sdk["ok"] = True
            sdk["error"] = None
        except Exception as e:
            sdk["ok"] = False
            sdk["error"] = repr(e)
        sdk["checked_at"] = time.time()
        return sdk


def read_odom(max_age=ODOM_MAX_AGE):
    with odom_lock:
        if not odom["ok"] or time.time() - odom["t"] > max_age:
            return None
        return dict(odom)


def wrap_pi(a):
    while a > math.pi:
        a -= 2 * math.pi
    while a < -math.pi:
        a += 2 * math.pi
    return a


def step_pose(x, y, h):
    with state_lock:
        state["pose"]["x"] = round(x, 3)
        state["pose"]["y"] = round(y, 3)
        state["pose"]["heading"] = round(h, 4)


def sleep_with_progress(seconds, index, total, on_tick=None):
    t0 = time.time()
    seconds = max(0.0, float(seconds))
    while True:
        elapsed = time.time() - t0
        if cancel_evt.is_set():
            return False
        local = 1.0 if seconds <= 0 else min(1.0, elapsed / seconds)
        set_progress(index, total, local)
        if on_tick is not None:
            on_tick(local)
        if not DRY_RUN:
            with state_lock:
                started = state["started_at"]
                last_poll = state["last_poll"]
            now = time.time()
            if started is not None and now - started > MAX_TASK_SECONDS:
                set_state(error="任务超过最长运行时间，已停止")
                cancel_evt.set()
            elif WATCHDOG > 0 and last_poll > 0 and now - last_poll > WATCHDOG:
                set_state(error="前端失联超过 %.0f 秒，已安全停止" % WATCHDOG)
                cancel_evt.set()
        if elapsed >= seconds:
            return True
        time.sleep(0.08)


def _stream_velocity(vx, vy, omega, state_obj):
    """Send a velocity command if the stream interval elapsed. Returns new last_send."""
    now = time.time()
    if now - state_obj["last_send"] >= STREAM_INTERVAL:
        if not DRY_RUN:
            code = loco.SetVelocity(vx, vy, omega, STREAM_DURATION)
            if code != 0:
                raise RuntimeError("SetVelocity 返回错误码 %s" % code)
        state_obj["last_send"] = now
    return state_obj["last_send"]


def move_block(kind, distance, index, total, speed, pose):
    x0, y0 = pose["x"], pose["y"]
    h = pose["heading"]
    dist = min(abs(float(distance)), MAX_DISTANCE)
    if kind == "move_forward":
        ux, uy = math.sin(h), math.cos(h)
        vx, vy = speed, 0.0
    elif kind == "move_back":
        ux, uy = -math.sin(h), -math.cos(h)
        vx, vy = -speed, 0.0
    elif kind == "move_left":
        ux, uy = -math.cos(h), math.sin(h)
        vx, vy = 0.0, speed * VY_LEFT_SIGN
    else:
        ux, uy = math.cos(h), -math.sin(h)
        vx, vy = 0.0, -speed * VY_LEFT_SIGN

    od = read_odom()
    ox0, oy0 = (od["x"], od["y"]) if od else (None, None)
    t0 = time.time()
    nominal = max(0.3, dist / max(0.2, speed))
    timeout = nominal * 2.5 + 3.0
    trav = 0.0
    st = {"last_send": 0.0}
    log("行走 %s %.2fm → vx=%.2f vy=%.2f，闭环=%s" % (kind, dist, vx, vy, "是" if od else "否"))
    try:
        while True:
            if cancel_evt.is_set():
                break
            now = time.time()
            if now - t0 > timeout:
                raise RuntimeError("行走超时（%.1fs），可能被阻挡或未进入运控" % timeout)
            _stream_velocity(vx, vy, 0.0, st)
            if ox0 is not None:
                od = read_odom()
                if od:
                    trav = math.hypot(od["x"] - ox0, od["y"] - oy0)
                if now - t0 > 2.5 and trav < 0.01:
                    raise RuntimeError("行走无响应：机器人没有产生位移，请确认处于走跑模式且未被阻挡")
            if ox0 is None:
                trav = min(dist, speed * (now - t0))
            local = max(0.0, min(1.0, trav / dist))
            step_pose(x0 + ux * trav, y0 + uy * trav, h)
            set_progress(index, total, local)
            if trav >= dist - 0.02:
                break
            time.sleep(0.04)
    finally:
        if not DRY_RUN:
            try:
                loco.StopMove()
            except Exception:
                pass
    if cancel_evt.is_set():
        return False
    time.sleep(0.3)
    pose["x"], pose["y"] = x0 + ux * trav, y0 + uy * trav
    step_pose(pose["x"], pose["y"], h)
    log("行走完成，实际位移 %.2fm" % trav)
    return True


def turn_block(kind, angle_deg, index, total, pose):
    angle = min(abs(float(angle_deg)), MAX_TURN_DEG) * math.pi / 180.0
    left = kind == "turn_left"
    h0 = pose["heading"]
    od = read_odom()
    yaw0 = od["yaw"] if od else None
    prev_yaw = yaw0
    net = 0.0
    omega = TURN_RATE if left else -TURN_RATE
    t0 = time.time()
    timeout = angle / TURN_RATE * 2.5 + 4.0
    st = {"last_send": 0.0}
    flipped = False
    log("转向 %s %.0f° → omega=%.2f，闭环=%s" % (kind, math.degrees(angle), omega, "是" if od else "否"))
    try:
        while True:
            if cancel_evt.is_set():
                break
            now = time.time()
            if now - t0 > timeout:
                if od and abs(net) < 0.02:
                    raise RuntimeError("转向无响应：机器人没有发生偏航，请确认处于走跑模式且未被阻挡")
                raise RuntimeError("转向超时（%.1fs）" % timeout)
            if od:
                cur = read_odom()
                if cur:
                    d = wrap_pi(cur["yaw"] - prev_yaw)
                    net += d
                    prev_yaw = cur["yaw"]
                    if not flipped and now - t0 > 0.8 and abs(net) > 0.04:
                        flipped = True
                        if (left and net < 0) or ((not left) and net > 0):
                            omega = -omega
                            log("转向方向与预期相反，已自动翻转 omega")
                    if now - t0 > 2.5 and abs(net) < 0.01:
                        raise RuntimeError("转向无响应：机器人没有发生偏航，请确认处于走跑模式且未被阻挡")
            else:
                net = (now - t0) * TURN_RATE * (1 if left else -1)
            _stream_velocity(0.0, 0.0, omega, st)
            step_pose(pose["x"], pose["y"], h0 - net)
            set_progress(index, total, max(0.0, min(1.0, abs(net) / angle)))
            reached = net >= angle - 0.02 if left else net <= -angle + 0.02
            if reached:
                break
            time.sleep(0.04)
    finally:
        if not DRY_RUN:
            try:
                loco.StopMove()
            except Exception:
                pass
    if cancel_evt.is_set():
        return False
    time.sleep(0.2)
    pose["heading"] = h0 - net
    step_pose(pose["x"], pose["y"], pose["heading"])
    log("转向完成，实测 %.1f°（目标 %.1f°）" % (math.degrees(abs(net)), math.degrees(angle)))
    return True


def arm_block(kind, index, total):
    mapping = {
        "wave_right": (23, 2.2, "right", "右臂抬起（right_hand_up）"),
        "wave_left": (15, 2.2, "left", "无独立左臂挥手，使用双手平举（both_hands_up）"),
        "wave_forward": (25, 2.8, "both", "胸前挥手（wave_under_head）"),
        "arm_down": (99, 1.0, "down", "恢复初始手臂位姿（release_arm）"),
        "reset": (99, 1.0, "down", "手臂复位（release_arm）"),
    }
    action_id, seconds, mode, note = mapping[kind]
    log("手臂 %s: ID %s, %s" % (kind, action_id, note))
    set_state(arm_mode=mode)
    if not DRY_RUN:
        code = arm.ExecuteAction(action_id)
        if code != 0:
            log("手臂动作返回错误码 %s（继续执行）" % code, code)
    return sleep_with_progress(seconds, index, total)


def speak_block(text, index, total):
    text = str(text or "")[:MAX_TEXT]
    if not text:
        return True
    duration = max(2.0, len(text) * 0.25 + 1.2)
    log("播报（约 %.1fs）：%s" % (duration, text))
    set_state(speech=text)
    if not DRY_RUN:
        try:
            audio.SetVolume(VOLUME)
        except Exception:
            pass
        code = audio.TtsMaker(text, SPEAKER_ID)
        if code != 0:
            log("TTS 返回错误码 %s（继续执行）" % code, code)
    ok = sleep_with_progress(duration, index, total)
    set_state(speech=None)
    return ok


def reset_pose():
    with state_lock:
        state["pose"] = {"x": 0.0, "y": 0.0, "heading": 0.0}


def worker(blocks, speed, source="http"):
    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
    total = len(blocks)
    reset_pose()
    set_state(running=True, index=-1, total=total, progress=0.0,
              block_progress=0.0, error=None, status="运行中",
              speech=None, arm_mode="down", started_at=time.time(),
              finished_at=None, last_poll=time.time(), source=source)
    try:
        for i, b in enumerate(blocks):
            if cancel_evt.is_set():
                break
            kind = b.get("type")
            value = b.get("value")
            set_state(index=i)
            set_progress(i, total, 0.0)
            if kind in ("move_forward", "move_back", "move_left", "move_right"):
                set_state(now=describe(b))
                if not move_block(kind, value, i, total, speed, pose):
                    break
            elif kind in ("turn_left", "turn_right"):
                set_state(now=describe(b))
                if not turn_block(kind, value, i, total, pose):
                    break
            elif kind in ("wave_right", "wave_left", "wave_forward", "arm_down", "reset"):
                set_state(now=describe(b))
                if not arm_block(kind, i, total):
                    break
                if kind == "reset":
                    pose = {"x": 0.0, "y": 0.0, "heading": 0.0}
                    reset_pose()
            elif kind == "speak":
                set_state(now="播报：" + str(value or ""))
                if not speak_block(value, i, total):
                    break
            elif kind == "wait":
                seconds = max(0.0, min(60.0, float(value or 0)))
                set_state(now="停顿 %.1f 秒" % seconds)
                if not sleep_with_progress(seconds, i, total):
                    break
            else:
                log("未知模块 %s，已跳过" % kind)
            set_progress(i, total, 1.0)

        if cancel_evt.is_set():
            set_state(status="已停止", now="已停止", index=-1)
            log("任务已停止")
        else:
            set_state(status="已完成", now="流程执行完成", progress=1.0, index=-1)
            log("任务执行完成")
    except Exception as e:
        set_state(status="执行失败", error=str(e), now="执行失败：%s" % e)
        log("执行失败: %r" % (e,))
        log(traceback.format_exc())
    finally:
        if not DRY_RUN:
            try:
                if loco is not None:
                    loco.StopMove()
            except Exception:
                pass
            try:
                if arm is not None:
                    arm.ExecuteAction(99)
            except Exception:
                pass
        set_state(running=False, speech=None, arm_mode="down", finished_at=time.time())
        cancel_evt.clear()


def describe(b):
    kind = b.get("type")
    v = b.get("value")
    names = {
        "move_forward": "向前走", "move_back": "向后走",
        "move_left": "向左走", "move_right": "向右走",
        "turn_left": "左转", "turn_right": "右转",
        "wave_right": "右臂挥手", "wave_left": "左臂挥手",
        "wave_forward": "向前挥手", "arm_down": "放下手臂",
        "reset": "复位", "speak": "播报", "wait": "停顿",
    }
    label = names.get(kind, kind)
    if kind in ("move_forward", "move_back", "move_left", "move_right"):
        return "%s %.2f m" % (label, float(v or 0))
    if kind in ("turn_left", "turn_right"):
        return "%s %.0f°" % (label, float(v or 0))
    if kind == "wait":
        return "%s %.1f 秒" % (label, float(v or 0))
    if kind == "speak":
        return "播报：" + str(v or "")
    return label


def validate_blocks(raw):
    if not isinstance(raw, list) or not raw:
        raise ValueError("指令列表为空")
    if len(raw) > MAX_BLOCKS:
        raise ValueError("指令过多（最多 %d 条）" % MAX_BLOCKS)
    allowed = {"move_forward", "move_back", "move_left", "move_right",
               "turn_left", "turn_right", "wave_right", "wave_left",
               "wave_forward", "arm_down", "reset", "speak", "wait"}
    blocks = []
    for item in raw:
        if not isinstance(item, dict) or item.get("type") not in allowed:
            raise ValueError("存在不支持的模块: %r" % (item,))
        b = {"type": item["type"], "value": item.get("value")}
        if b["type"] in ("move_forward", "move_back", "move_left", "move_right"):
            v = float(b["value"] or 0)
            if v <= 0 or v > MAX_DISTANCE:
                raise ValueError("单段距离需在 0~%.1fm 之间" % MAX_DISTANCE)
            b["value"] = v
        elif b["type"] in ("turn_left", "turn_right"):
            v = abs(float(b["value"] or 0))
            if v <= 0 or v > MAX_TURN_DEG:
                raise ValueError("转角需在 0~%.0f° 之间" % MAX_TURN_DEG)
            b["value"] = v
        elif b["type"] == "wait":
            b["value"] = max(0.0, min(60.0, float(b["value"] or 0)))
        elif b["type"] == "speak":
            b["value"] = str(b["value"] or "")[:MAX_TEXT]
        blocks.append(b)
    return blocks


def start_task(raw_blocks, speed, source="http"):
    with state_lock:
        if state["running"]:
            return False, "已有任务正在执行", None, 409
    try:
        blocks = validate_blocks(raw_blocks)
    except (ValueError, TypeError) as e:
        return False, str(e), None, 400
    try:
        speed = float(speed)
    except (TypeError, ValueError):
        speed = 0.5
    speed = max(0.2, min(MAX_SPEED, speed))

    if not DRY_RUN and not sdk["ok"]:
        try_init()
    if not sdk["ok"]:
        return False, "SDK 未就绪: %s" % (sdk["error"] or "unknown"), None, 503

    if not DRY_RUN:
        refresh_status(force=True)
        fsm = sdk.get("fsm_id")
        mode = sdk.get("fsm_mode")
        if fsm not in WALK_FSM:
            return False, ("机器人当前 FSM=%s，不是走跑运控（801/802）。请先用遥控器切到走跑/站立模式。" % fsm), None, 409
        if mode != 0:
            return False, ("机器人当前处于动态状态（fsm_mode=%s），请等它站稳后再执行。" % mode), None, 409
        try:
            if audio is not None:
                audio.SetVolume(VOLUME)
                sdk["volume"] = VOLUME
        except Exception:
            pass

    cancel_evt.clear()
    with state_lock:
        state["task_id"] += 1
        tid = state["task_id"]
    log("接收任务 #%d（%s）：%d 条指令，步速 %.2f m/s" % (tid, source, len(blocks), speed))
    threading.Thread(target=worker, args=(blocks, speed, source), daemon=True).start()
    return True, "", tid, 200


def safe_workflow_name(name):
    name = str(name or "").strip()
    if not name:
        raise ValueError("工作流名称不能为空")
    if len(name) > 40:
        raise ValueError("工作流名称过长")
    if any(c in name for c in '/\\:*?"<>|\0'):
        raise ValueError("工作流名称包含非法字符")
    return name


def save_workflow(name, blocks, speed):
    path = os.path.join(WORKFLOW_DIR, name + ".json")
    payload = {"name": name, "blocks": blocks, "speed": speed,
               "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    return payload


def save_current():
    with state_lock:
        payload = {"name": voice_workflow["name"], "blocks": voice_workflow["blocks"],
                   "speed": voice_workflow["speed"],
                   "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(CURRENT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)


def load_current():
    global voice_workflow
    try:
        with open(CURRENT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        blocks = validate_blocks(data.get("blocks") or [])
        voice_workflow = {"blocks": blocks,
                          "speed": max(0.2, min(MAX_SPEED, float(data.get("speed") or 0.5))),
                          "name": str(data.get("name") or "")}
        log("已载入当前工作流：%s（%d 条）" % (voice_workflow["name"] or "(未命名)", len(blocks)))
    except FileNotFoundError:
        pass
    except Exception as e:
        log("载入当前工作流失败: %r" % (e,))


class Handler(BaseHTTPRequestHandler):
    server_version = "G1Card/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            n = int(self.headers.get("Content-Length", "0"))
        except Exception:
            n = 0
        if n <= 0 or n > 1024 * 1024:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            raise ValueError("请求体不是合法 JSON")

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/status":
            with state_lock:
                running = state["running"]
            if not DRY_RUN and not sdk["ok"] and not running:
                try_init()
            if not DRY_RUN and sdk["ok"] and not running:
                refresh_status()
            s = dict(sdk)
            with state_lock:
                s["running"] = state["running"]
                s["status"] = state["status"]
            s["voice_enabled"] = voice["enabled"]
            return self._json(s)

        if path == "/api/state":
            with state_lock:
                state["last_poll"] = time.time()
                out = json.loads(json.dumps(state, ensure_ascii=False))
            return self._json(out)

        if path == "/api/actions":
            return self._json({"ok": sdk["actions"] is not None, "actions": sdk["actions"]})

        if path == "/api/voice":
            with asr_lock:
                a = dict(asr)
            a["enabled"] = voice["enabled"]
            a["keywords"] = VOICE_KEYWORDS
            with state_lock:
                a["workflow_name"] = voice_workflow["name"]
                a["workflow_blocks"] = len(voice_workflow["blocks"])
            return self._json(a)

        if path == "/api/workflows":
            items = []
            try:
                for fn in sorted(os.listdir(WORKFLOW_DIR)):
                    if not fn.endswith(".json") or fn.startswith("_"):
                        continue
                    try:
                        with open(os.path.join(WORKFLOW_DIR, fn), "r", encoding="utf-8") as f:
                            d = json.load(f)
                        items.append({"name": d.get("name") or fn[:-5],
                                      "saved_at": d.get("saved_at"),
                                      "blocks": len(d.get("blocks") or []),
                                      "speed": d.get("speed")})
                    except Exception:
                        pass
            except FileNotFoundError:
                pass
            return self._json({"ok": True, "items": items})

        if path == "/api/workflows/get":
            name = (qs.get("name") or [""])[0]
            try:
                name = safe_workflow_name(name)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            fp = os.path.join(WORKFLOW_DIR, name + ".json")
            if not os.path.isfile(fp):
                return self._json({"ok": False, "error": "工作流不存在"}, 404)
            with open(fp, "r", encoding="utf-8") as f:
                return self._json({"ok": True, "workflow": json.load(f)})

        if path == "/api/health":
            return self._json({"ok": True, "pid": os.getpid()})
        return self._static(path)

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            payload = self._read_json()
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)

        if path == "/api/run":
            if not payload.get("confirm"):
                return self._json({"ok": False, "error": "缺少现场安全确认 (confirm=true)"}, 400)
            ok, err, tid, code = start_task(payload.get("blocks"), payload.get("speed", 0.5), "http")
            if not ok:
                return self._json({"ok": False, "error": err}, code)
            with state_lock:
                n = len(state["log"])
            return self._json({"ok": True, "task_id": tid, "blocks": len(payload.get("blocks") or []),
                               "speed": payload.get("speed", 0.5)})

        if path == "/api/stop":
            with state_lock:
                running = state["running"]
            if not running:
                return self._json({"ok": True, "stopping": False})
            cancel_evt.set()
            log("收到停止请求")
            return self._json({"ok": True, "stopping": True})

        if path == "/api/workflows":
            try:
                name = safe_workflow_name(payload.get("name"))
                blocks = validate_blocks(payload.get("blocks"))
            except (ValueError, TypeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            try:
                speed = max(0.2, min(MAX_SPEED, float(payload.get("speed") or 0.5)))
            except (TypeError, ValueError):
                speed = 0.5
            data = save_workflow(name, blocks, speed)
            log("保存工作流：%s（%d 条）" % (name, len(blocks)))
            return self._json({"ok": True, "workflow": data})

        if path == "/api/current":
            try:
                blocks = validate_blocks(payload.get("blocks"))
            except (ValueError, TypeError) as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            try:
                speed = max(0.2, min(MAX_SPEED, float(payload.get("speed") or 0.5)))
            except (TypeError, ValueError):
                speed = 0.5
            with state_lock:
                voice_workflow["blocks"] = blocks
                voice_workflow["speed"] = speed
                voice_workflow["name"] = str(payload.get("name") or "")
            save_current()
            return self._json({"ok": True, "blocks": len(blocks)})

        if path == "/api/voice":
            if "enabled" in payload:
                voice["enabled"] = bool(payload.get("enabled"))
                log("语音启动：%s" % ("开启" if voice["enabled"] else "关闭"))
            return self._json({"ok": True, "enabled": voice["enabled"]})

        if path == "/api/voice/trigger":
            with state_lock:
                blocks = list(voice_workflow["blocks"])
                speed = voice_workflow["speed"]
            if not blocks:
                return self._json({"ok": False, "error": "当前没有同步工作流"}, 400)
            ok, err, tid, code = start_task(blocks, speed, "voice-test")
            if not ok:
                return self._json({"ok": False, "error": err}, code)
            with asr_lock:
                asr["trigger_count"] += 1
                asr["last_trigger_t"] = time.time()
                asr["last_trigger_text"] = "(test)"
            return self._json({"ok": True, "task_id": tid})

        return self._json({"ok": False, "error": "not found"}, 404)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path != "/api/workflows":
            return self._json({"ok": False, "error": "not found"}, 404)
        qs = parse_qs(parsed.query)
        try:
            name = safe_workflow_name((qs.get("name") or [""])[0])
        except ValueError as e:
            return self._json({"ok": False, "error": str(e)}, 400)
        fp = os.path.join(WORKFLOW_DIR, name + ".json")
        if not os.path.isfile(fp):
            return self._json({"ok": False, "error": "工作流不存在"}, 404)
        os.remove(fp)
        log("删除工作流：%s" % name)
        return self._json({"ok": True})

    def _static(self, path):
        if path in ("/", ""):
            path = "/index.html"
        rel = unquote(path).lstrip("/")
        full = os.path.realpath(os.path.join(WEB_DIR, rel))
        if not full.startswith(os.path.realpath(WEB_DIR) + os.sep) or not os.path.isfile(full):
            return self._json({"ok": False, "error": "not found"}, 404)
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
        }.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
        with open(full, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


def main():
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    log("G1 指令卡后端启动，端口 %d，接口 %s，dry_run=%s，音量=%d" % (PORT, IFACE, DRY_RUN, VOLUME))
    load_current()
    init_sdk()
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    srv.daemon_threads = True
    log("HTTP 服务已启动: http://0.0.0.0:%d/" % PORT)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
