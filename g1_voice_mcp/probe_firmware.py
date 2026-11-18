#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实机只读/低风险探测：确认这台固件的 FSM 语义（蹲姿 706、起身、限时移动）。

用法（在机器人上，有人看护、机器人站稳、周边留空）：
  PYTHONPATH=/home/unitree/huang/unitree-app-templates/G1_joystick/vendor \
  LD_LIBRARY_PATH=/home/unitree/huang/unitree-app-templates/G1_joystick/out/lib \
  python3.8 probe_firmware.py --step status

  python3.8 probe_firmware.py --step squat     # 蹲下探测（会动）
  python3.8 probe_firmware.py --step stand     # 起身探测（会动）
  python3.8 probe_firmware.py --step move      # 0.1m/s 0.5s 前进探测（会动）

安全约束：
  - 默认只读；--step 动作需要显式指定，且带 --yes 才执行。
  - 只在 FSM=802（走跑/站立）时动作；其余状态直接拒绝。
  - 每个动作结束都会发 StopMove；异常/中断同样兜底。
"""

import argparse
import json
import sys
import time

FSM_NAMES = {
    0: "零力矩", 1: "阻尼", 2: "蹲姿(旧码)", 3: "坐姿", 4: "预备",
    500: "启动", 501: "行走", 503: "舞蹈模式", 702: "趴姿", 706: "蹲姿",
    802: "走跑/站立", 1000: "用户控制(策略执行中)",
}


def fsm_text(v):
    return "%s(%s)" % (v, FSM_NAMES.get(v, "未知"))


class Loco(object):
    def __init__(self, iface):
        from unitree_sdk2py.core.channel import ChannelFactoryInitialize
        from unitree_sdk2py.g1.loco.g1_loco_client import LocoClient
        ChannelFactoryInitialize(0, iface)
        self.c = LocoClient()
        self.c.SetTimeout(5.0)
        self.c.Init()

    def fsm(self):
        try:
            code, data = self.c._Call(7001, "{}")
            if code == 0 and data:
                return json.loads(data).get("data")
        except Exception as e:
            print("GetFsmId 异常: %s" % e)
        return None

    def height(self):
        try:
            code, data = self.c._Call(7005, "{}")
            if code == 0 and data:
                return json.loads(data).get("data")
        except Exception:
            pass
        return None

    def set_fsm(self, v):
        return self.c.SetFsmId(int(v))

    def velocity(self, vx, vy, vyaw, dur):
        return self.c.SetVelocity(vx, vy, vyaw, dur)

    def stop(self):
        try:
            self.c.SetVelocity(0.0, 0.0, 0.0, 1.0)
        except Exception:
            pass


def wait_for(loco, targets, timeout):
    t0 = time.time()
    while time.time() - t0 < timeout:
        cur = loco.fsm()
        if cur in targets:
            return cur
        time.sleep(0.15)
    return None


def require_run(loco):
    cur = loco.fsm()
    if cur != 802:
        print("拒绝：当前 %s，只允许在 802(走跑/站立) 下探测" % fsm_text(cur))
        return False
    return True


def step_status(loco):
    print("FSM        = %s" % fsm_text(loco.fsm()))
    print("站高        = %s" % loco.height())
    return 0


def step_squat(loco):
    if not require_run(loco):
        return 2
    h0 = loco.height()
    print("发送 SetFsmId(706) ... 起始站高=%s" % h0)
    code = loco.set_fsm(706)
    print("RPC code = %s" % code)
    got = wait_for(loco, (706, 2), 4.0)
    time.sleep(1.0)
    h1 = loco.height()
    print("4s 后 FSM = %s，站高 = %s" % (fsm_text(loco.fsm()), h1))
    if got is not None:
        print("结论：706 使 FSM 变为 %s → 蹲姿由 FSM 码表达" % fsm_text(got))
        return 0
    if h0 is not None and h1 is not None and h0 - h1 > 0.02:
        print("结论：FSM 未变但站高下降 %.3f m → 706 为身高式蹲姿" % (h0 - h1))
        return 0
    print("结论：706 未见效果，该固件不支持此蹲姿指令")
    return 3


def step_stand(loco):
    cur = loco.fsm()
    print("当前 FSM = %s" % fsm_text(cur))
    if cur == 802:
        print("已经是站立状态，无需动作")
        return 0
    # 实机结论：恢复序列为 阻尼(1) -> 706 -> 802（约 6.5s）。
    # 本固件 702 是"趴/蹲"状态而非起身指令；4/500 也无效，500 会进阻尼。
    if loco.fsm() != 1:
        print("先进入阻尼(1) ...")
        print(" RPC code = %s" % loco.set_fsm(1))
        wait_for(loco, (1,), 6.0)
    print("尝试 SetFsmId(706) ...")
    code = loco.set_fsm(706)
    print("RPC code = %s" % code)
    if wait_for(loco, (802,), 15.0) is not None:
        print("结论：阻尼 -> 706 可让机器人回到 802")
        return 0
    print("结论：自动起身失败（当前 %s），请用遥控器" % fsm_text(loco.fsm()))
    return 3


def step_move(loco):
    if not require_run(loco):
        return 2
    print("发送 SetVelocity(0.1, 0, 0, 864000) 并 0.5s 后 StopMove ...")
    try:
        code = loco.velocity(0.1, 0.0, 0.0, 864000.0)
        print("RPC code = %s" % code)
        time.sleep(0.5)
    finally:
        loco.stop()
        print("已 StopMove")
    print("请目视确认：机器人是否只前移了一小段后停住")
    return 0


STEPS = {"status": step_status, "squat": step_squat, "stand": step_stand, "move": step_move}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--step", choices=sorted(STEPS), default="status")
    ap.add_argument("--yes", action="store_true", help="确认真要执行动作")
    args = ap.parse_args()

    if args.step != "status" and not args.yes:
        print("动作类探测需要加 --yes 明确确认（机器人会动）")
        return 2

    loco = Loco(args.iface)
    try:
        return STEPS[args.step](loco)
    finally:
        loco.stop()


if __name__ == "__main__":
    sys.exit(main())
