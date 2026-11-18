#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
移动控制低速验证（会动机器人，必须 --yes）。

只做最小动作：0.1 m/s × 0.5 s（约 5 cm），验证 Move + 服务端定时 StopMove。
前置：机器人必须 FSM=802；dances.json 的 safety.allow_move 必须为 true。

用法：
  PYTHONPATH=.../vendor LD_LIBRARY_PATH=.../out/lib \
  python3.8 test_live_move.py --yes
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
import os

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

    def call(self, name, args=None, timeout=40):
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
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--vx", type=float, default=0.1)
    ap.add_argument("--duration", type=float, default=0.5)
    args = ap.parse_args()
    if not args.yes:
        print("会动机器人，请加 --yes（站稳、周边留空、有人看护）")
        return 2

    c = Client()
    try:
        err, txt = c.call("robot_status", timeout=30)
        show("preflight", err, txt)
        if "802" not in txt:
            print("前置检查失败：不在 FSM=802，拒绝移动")
            return 3

        show("move", *c.call("move", {"vx": args.vx, "vy": 0, "vyaw": 0,
                                      "duration_s": args.duration}, timeout=30))
        time.sleep(2.0)
        show("status after", *c.call("robot_status", timeout=30))
        show("move_stop", *c.call("move_stop", timeout=30))
        time.sleep(1.0)
        show("final", *c.call("robot_status", timeout=30))
    finally:
        c.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
