#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
小野AI MCP 接入点的出站 WebSocket 桥。

  wss://api.xiaozhi.me/mcp/?token=...   <-- 本进程作为 WebSocket 客户端出站连接
                |
        stdio（换行分隔 JSON-RPC）
                |
        g1_mcp_server.py（python3.8 + G1_joystick/vendor）

设计要点：
  - 不依赖 mcp SDK / mcp-proxy，只需要 websockets（机器人系统 python3.8 已自带 11.0.3）。
  - 断线指数退避重连（1s -> 600s），连接稳定后重置退避。
  - 令牌只从环境变量 MCP_ENDPOINT 读取，日志里只打印 scheme://host/path，不打印 token。
  - 子进程 stderr 直接继承，交给 systemd/journal 记录；stdout 只用于 MCP 报文。
"""

import argparse
import asyncio
import os
import shlex
import sys
from urllib.parse import urlsplit

import websockets

HERE = os.path.dirname(os.path.abspath(__file__))

INITIAL_BACKOFF = 1.0
MAX_BACKOFF = 600.0
STABLE_AFTER = 60.0


def log(msg):
    print("[xz-pipe] %s" % msg, file=sys.stderr, flush=True)


def safe_endpoint(uri):
    """日志用：去掉 query（token 在其中）。"""
    try:
        p = urlsplit(uri)
        return "%s://%s%s" % (p.scheme, p.netloc, p.path)
    except Exception:
        return "<invalid endpoint>"


async def ws_to_proc(ws, proc):
    async for message in ws:
        if isinstance(message, bytes):
            message = message.decode("utf-8", "replace")
        line = message.strip()
        if not line:
            continue
        proc.stdin.write((line + "\n").encode("utf-8"))
        await proc.stdin.drain()


async def proc_to_ws(proc, ws):
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise ConnectionError("stdio 服务已退出")
        line = line.strip()
        if not line:
            continue
        await ws.send(line.decode("utf-8", "replace"))


def build_command(args):
    if args.server_cmd:
        return shlex.split(args.server_cmd)
    return [sys.executable, os.path.join(HERE, "g1_mcp_server.py"), "--iface", args.iface]


async def run_once(endpoint, command):
    async with websockets.connect(
        endpoint,
        ping_interval=20,
        ping_timeout=20,
        close_timeout=5,
        max_size=4 * 1024 * 1024,
    ) as ws:
        log("已连接 %s" % safe_endpoint(endpoint))
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # 继承到 journal
        )
        log("已启动 stdio 服务：%s (pid=%s)" % (" ".join(command), proc.pid))

        tasks = [
            asyncio.create_task(ws_to_proc(ws, proc)),
            asyncio.create_task(proc_to_ws(proc, ws)),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)

        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)

        err = None
        for task in done:
            try:
                task.result()
            except Exception as e:
                err = e
        if proc.returncode is None:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=3)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        if err:
            raise err
        raise ConnectionError("连接或子进程已结束")


async def main_async(args):
    endpoint = os.environ.get("MCP_ENDPOINT", "").strip()
    if not endpoint:
        log("错误：环境变量 MCP_ENDPOINT 未设置（应为接入点的 wss 地址）")
        return 2

    command = build_command(args)
    log("目标接入点：%s；服务命令：%s" % (safe_endpoint(endpoint), " ".join(command)))

    backoff = INITIAL_BACKOFF
    while True:
        started = asyncio.get_event_loop().time()
        try:
            await run_once(endpoint, command)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log("连接中断：%s" % e)

        elapsed = asyncio.get_event_loop().time() - started
        if elapsed >= STABLE_AFTER:
            backoff = INITIAL_BACKOFF
        log("将在 %.0f 秒后重连" % backoff)
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, MAX_BACKOFF)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--iface", default="eth0", help="传给 g1_mcp_server.py 的 DDS 网卡")
    ap.add_argument("--server-cmd", default=None, help="覆盖 stdio 服务启动命令")
    args = ap.parse_args()
    try:
        asyncio.get_event_loop().run_until_complete(main_async(args))
    except KeyboardInterrupt:
        log("收到 Ctrl-C，退出")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
