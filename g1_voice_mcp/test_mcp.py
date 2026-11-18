#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地自测：
  1) stdio：直接驱动 g1_mcp_server.py --simulate，验证协议与 8 个工具逻辑。
  2) websocket：起一个本地 mock 云端，跑 xz_pipe.py，验证完整链路（不含 DDS）。

用法：
  python3 test_mcp.py            # 跑两项
  python3 test_mcp.py --no-ws    # 只跑 stdio
"""

import argparse
import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SERVER = os.path.join(HERE, "g1_mcp_server.py")
PIPE = os.path.join(HERE, "xz_pipe.py")

failures = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        failures.append(name)


class StdioClient(object):
    def __init__(self, simulate=True):
        cmd = [sys.executable, SERVER]
        if simulate:
            cmd.append("--simulate")
        self.proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, bufsize=0,
        )
        self.q = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.q.put(json.loads(line.decode("utf-8")))
            except Exception as e:
                print("  非 JSON 输出：%r (%s)" % (line[:200], e))

    def send(self, obj):
        self.proc.stdin.write((json.dumps(obj) + "\n").encode("utf-8"))
        self.proc.stdin.flush()

    def request(self, obj, timeout=12):
        self.send(obj)
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                msg = self.q.get(timeout=max(0.05, deadline - time.time()))
            except queue.Empty:
                break
            if msg.get("id") == obj.get("id"):
                return msg
        raise TimeoutError("等待 id=%s 超时" % obj.get("id"))

    def close(self):
        try:
            self.proc.terminate()
        except Exception:
            pass


def test_stdio():
    print("== stdio 协议与工具（simulate） ==")
    c = StdioClient()
    try:
        init = c.request({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05",
                                     "capabilities": {},
                                     "clientInfo": {"name": "test", "version": "1"}}})
        check("initialize", init.get("result", {}).get("protocolVersion") in
              ("2024-11-05", "2025-03-26", "2025-06-18"), init)
        c.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        check("ping", c.request({"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}})
              .get("result") == {})

        tl = c.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = tl.get("result", {}).get("tools", [])
        size = len(json.dumps(tl.get("result"), ensure_ascii=False).encode("utf-8"))
        check("tools 数量 = 8", len(tools) == 8, "got %d" % len(tools))
        check("tools/list < 8192 bytes", size < 8192, "%d bytes" % size)
        print("  tools/list 报文大小：%d bytes" % size)

        def call(i, name, args=None):
            time.sleep(0.9)  # 尊重服务端 0.8s 限流（robot_status/dance_list 不受限）
            return c.request({"jsonrpc": "2.0", "id": i, "method": "tools/call",
                              "params": {"name": name, "arguments": args or {},
                                         "serialNumber": None}})

        r = call(3, "robot_status")
        check("robot_status", "状态" in r["result"]["content"][0]["text"], r)
        r = call(4, "dance_list")
        txt = r["result"]["content"][0]["text"]
        check("dance_list 含 8 行", len([x for x in txt.splitlines() if x.strip()]) == 8, txt)
        r = call(5, "dance_play", {"dance": "江南Style"})
        check("dance_play 江南Style", r["result"]["isError"] is False and "江南" in
              r["result"]["content"][0]["text"], r)
        r = call(6, "dance_stop")
        check("dance_stop", r["result"]["isError"] is False, r)
        r = call(7, "squat")
        check("squat 被安全停用", r["result"]["isError"] is True and "停用" in
              r["result"]["content"][0]["text"], r)
        r = call(8, "stand_up")
        check("stand_up 被安全停用", r["result"]["isError"] is True and "停用" in
              r["result"]["content"][0]["text"], r)
        r = call(9, "move", {"vx": 0.2, "vy": 0, "vyaw": 0, "duration_s": 0.5})
        allow_move = json.load(open(os.path.join(HERE, "dances.json"), encoding="utf-8")) \
            .get("safety", {}).get("allow_move", False)
        if allow_move:
            check("move 已启用且执行成功", r["result"]["isError"] is False, r)
        else:
            check("move 默认停用", r["result"]["isError"] is True and "停用" in
                  r["result"]["content"][0]["text"], r)
        r = call(10, "move_stop")
        check("move_stop", r["result"]["isError"] is False, r)
        r = call(11, "dance_play", {"dance": "不存在的舞蹈"})
        check("未知舞蹈被拒绝", r["result"]["isError"] is True, r)
        r = call(12, "dance_play", {"dance": "机械舞1"})
        check("别名 机械舞1", r["result"]["isError"] is False, r)
        r = call(13, "dance_stop")
        check("收尾 dance_stop", r["result"]["isError"] is False, r)
    finally:
        c.close()


async def test_ws():
    print("== xz_pipe ↔ mock 云端 ==")
    try:
        from websockets.asyncio.server import serve
    except ImportError:  # websockets 11
        from websockets.server import serve

    results = {}
    done = asyncio.Event()

    async def rpc(ws, obj, timeout=12):
        await ws.send(json.dumps(obj))
        while True:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout))
            if msg.get("id") == obj.get("id"):
                return msg

    async def handler(ws, path=None):
        try:
            results["init"] = await rpc(ws, {
                "jsonrpc": "2.0", "id": 0, "method": "initialize",
                "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                           "clientInfo": {"name": "xz-mcp-broker", "version": "0.0.1"}}})
            await ws.send(json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}))
            results["tools"] = await rpc(ws, {
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
            results["call"] = await rpc(ws, {
                "jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "robot_status", "arguments": {}, "serialNumber": None}})
            results["ping"] = await rpc(ws, {
                "jsonrpc": "2.0", "id": 4, "method": "ping", "params": {}})
        except Exception as e:
            results["error"] = repr(e)
        finally:
            done.set()

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    env = dict(os.environ)
    env["MCP_ENDPOINT"] = "ws://127.0.0.1:%d/mcp/?token=test-token" % port
    cmd = [sys.executable, PIPE,
           "--server-cmd", "%s %s --simulate" % (sys.executable, SERVER)]
    proc = subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL)
    try:
        await asyncio.wait_for(done.wait(), timeout=30)
    except asyncio.TimeoutError:
        results["error"] = "timeout"
    finally:
        proc.terminate()
        try:
            await asyncio.wait_for(asyncio.to_thread(proc.wait), timeout=3)
        except Exception:
            proc.kill()
        server.close()
        await server.wait_closed()

    check("ws initialize 转发", results.get("init", {}).get("result", {})
          .get("protocolVersion") in ("2024-11-05", "2025-03-26", "2025-06-18"),
          results.get("init") or results.get("error"))
    tools = results.get("tools", {}).get("result", {}).get("tools", [])
    check("ws tools/list = 8", len(tools) == 8, "%d" % len(tools))
    body = results.get("call", {}).get("result", {})
    check("ws tools/call", body.get("isError") is False, results.get("call"))
    check("ws ping", results.get("ping", {}).get("result") == {}, results.get("ping"))


def test_live_readonly():
    """真实 DDS 模式下的只读联调：只调用 robot_status / dance_list，不触发任何动作。"""
    print("== 实机只读联调（不触发动作） ==")
    c = StdioClient(simulate=False)
    try:
        init = c.request({"jsonrpc": "2.0", "id": 0, "method": "initialize",
                          "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                     "clientInfo": {"name": "live-test", "version": "1"}}})
        check("initialize", init.get("result", {}).get("serverInfo", {}).get("name") == "g1-robot", init)
        tl = c.request({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        check("tools/list", len(tl.get("result", {}).get("tools", [])) == 8, tl)

        r = c.request({"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                       "params": {"name": "robot_status", "arguments": {}}}, timeout=25)
        txt = r["result"]["content"][0]["text"]
        print("  robot_status -> %s" % txt)
        check("robot_status 可读 FSM", "未知" not in txt, txt)

        r = c.request({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                       "params": {"name": "dance_list", "arguments": {}}}, timeout=20)
        check("dance_list", r["result"]["isError"] is False, r)
    finally:
        c.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-ws", action="store_true")
    ap.add_argument("--live", action="store_true", help="额外跑实机只读联调（需要 DDS）")
    args = ap.parse_args()

    test_stdio()
    if not args.no_ws:
        asyncio.run(test_ws())
    if args.live:
        test_live_readonly()

    print()
    if failures:
        print("失败项：%s" % ", ".join(failures))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
