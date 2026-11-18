#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
实机验证（会动机器人，必须显式 --yes）。

安全策略（2026-11-17 蹲姿摔倒事故后）：
  - 只验证舞蹈（按键注入，与实体遥控器同一通道，已在实机长期使用）。
  - squat / stand_up / move 默认停用，不做任何自动姿态/移动测试。
  - 前置检查：机器人必须处于 FSM=802（站立/走跑），否则直接退出。

用法：
  PYTHONPATH=.../G1_joystick/vendor LD_LIBRARY_PATH=.../out/lib \
  python3.8 test_live_dance.py --yes --dance Su
"""

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "g1_mcp_server.py")


class Client(object):
    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, SERVER], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=None, bufsize=0)
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.n = 0

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if line:
                try:
                    self.q.put(json.loads(line.decode("utf-8")))
                except Exception:
                    pass

    def call(self, name, args=None, timeout=60):
        self.n += 1
        rid = self.n
        self.proc.stdin.write((json.dumps({
            "jsonrpc": "2.0", "id": rid, "method": "tools/call",
            "params": {"name": name, "arguments": args or {}, "serialNumber": None},
        }) + "\n").encode())
        self.proc.stdin.flush()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == rid:
                res = msg.get("result") or {}
                text = "".join(c.get("text", "") for c in res.get("content", []))
                return bool(res.get("isError")), text
        return True, "TIMEOUT"

    def close(self):
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def show(label, err, text):
    print("[%s] %s %s" % (label, "ERROR" if err else "OK", text))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="确认机器人会动")
    ap.add_argument("--dance", default="Su", help="要验证的舞蹈，默认 Su（5.1s，最短最安全）")
    ap.add_argument("--stop-after", type=float, default=0.0, help="播放 N 秒后调用 dance_stop（验证打断）")
    ap.add_argument("--timeout", type=float, default=150.0, help="等待动作结束的最长秒数")
    args = ap.parse_args()

    if not args.yes:
        print("会动机器人，请加 --yes 确认（机器人需站稳、周边留空、有人看护）")
        return 2

    c = Client()
    try:
        err, txt = c.call("robot_status", timeout=30)
        show("preflight", err, txt)
        if "802" not in txt:
            print("前置检查失败：机器人不在 FSM=802（站立/走跑），拒绝执行任何动作")
            return 3

        print("\n--- dance %s ---" % args.dance)
        show("dance_play", *c.call("dance_play", {"dance": args.dance}, timeout=60))
        time.sleep(2.0)
        show("status", *c.call("robot_status", timeout=30))

        if args.stop_after > 0:
            print("\n--- stop after %.1fs ---" % args.stop_after)
            time.sleep(args.stop_after)
            show("dance_stop", *c.call("dance_stop", timeout=60))
            time.sleep(1.0)
            show("status", *c.call("robot_status", timeout=30))
            return 0

        deadline = time.time() + args.timeout
        while time.time() < deadline:
            time.sleep(3)
            err, txt = c.call("robot_status", timeout=30)
            if "空闲" in txt:
                break
        show("final", *c.call("robot_status", timeout=30))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
