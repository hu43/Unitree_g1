#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测量 BGM “事件 -> 实际出声” 延迟。

原理：
  - 订阅 rt/g1joystick/event，记录 start:<状态> 的墙钟时间；
  - 同时接收机器人麦克风组播 (239.168.123.161:5555, 16k s16le)，
    逐包计算 RMS 与接收时间；
  - 比较两者，得到真正出声时刻。

用法（机器人上，需站稳、周边安静、有人看护）：
  PYTHONPATH=.../G1_joystick/vendor LD_LIBRARY_PATH=.../out/lib \
  python3.8 measure_audio.py --yes --dance Gangnam --record 12 --stop-at 8
"""

import argparse
import array
import math
import socket
import struct
import sys
import threading
import time

GROUP_IP = "239.168.123.161"
PORT = 5555
SR = 16000

pcm_packets = []          # (recv_time, rms, n_samples)
events = []               # (recv_time, text)
recording = True
lock = threading.Lock()


def rms_of(data):
    a = array.array("h")
    try:
        a.frombytes(data)
    except ValueError:
        return 0.0
    if not a:
        return 0.0
    return math.sqrt(sum(float(x) * x for x in a) / len(a))


def mic_thread(local_ip):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
    except (AttributeError, OSError):
        pass
    s.bind(("", PORT))
    mreq = struct.pack("4s4s", socket.inet_aton(GROUP_IP), socket.inet_aton(local_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    s.settimeout(0.5)
    print("[mic] joined %s:%d on %s" % (GROUP_IP, PORT, local_ip))
    while recording:
        try:
            data, _ = s.recvfrom(4096)
        except socket.timeout:
            continue
        except Exception as e:
            print("[mic] %s" % e)
            break
        if len(data) >= 2:
            with lock:
                pcm_packets.append((time.time(), rms_of(data), len(data) // 2))
    try:
        s.close()
    except Exception:
        pass


def main():
    global recording
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true")
    ap.add_argument("--dance", default="Gangnam")
    ap.add_argument("--record", type=float, default=12.0)
    ap.add_argument("--stop-at", type=float, default=8.0)
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--threshold", type=float, default=300.0)
    ap.add_argument("--inject", action="store_true",
                    help="只向 rt/g1joystick/event 注入事件（机器人不动），隔离音频链路延迟")
    args = ap.parse_args()
    if not args.yes:
        print("会动机器人，请加 --yes")
        return 2

    from unitree_sdk2py.core.channel import (
        ChannelFactoryInitialize, ChannelPublisher, ChannelSubscriber)
    from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_

    ChannelFactoryInitialize(0, args.iface)

    if args.inject:
        pub = ChannelPublisher("rt/g1joystick/event", String_)
        pub.Init()
        start_text = "start:" + args.dance
        stop_text = "stop:" + args.dance
    else:
        pub = ChannelPublisher("rt/g1mcp/cmd", String_)
        pub.Init()
        start_text = args.dance
        stop_text = "stop"

    def on_event(msg):
        with lock:
            events.append((time.time(), getattr(msg, "data", "") or ""))

    sub = ChannelSubscriber("rt/g1joystick/event", String_)
    sub.Init(on_event)

    local_ip = "192.168.123.164"
    t = threading.Thread(target=mic_thread, args=(local_ip,), daemon=True)
    t.start()
    time.sleep(2.0)  # 基线

    start = time.time()
    print("[cmd] %s at t=0.000 (inject=%s)" % (start_text, args.inject))
    pub.Write(String_(data=start_text))

    stopped = False
    while time.time() - start < args.record:
        el = time.time() - start
        if not stopped and args.stop_at > 0 and el >= args.stop_at:
            print("[cmd] %s at t=%.3f" % (stop_text, el))
            pub.Write(String_(data=stop_text))
            stopped = True
        time.sleep(0.05)

    recording = False
    time.sleep(0.3)

    with lock:
        ev = [e for e in events if e[0] >= start - 0.05]
        pk = [p for p in pcm_packets if p[0] >= start - 1.0]

    print("\n=== events ===")
    for ts, txt in ev:
        print("  +%.3fs  %s" % (ts - start, txt))

    ev_t = next((ts for ts, txt in ev if txt.startswith("start:")), None)
    if ev_t is None:
        print("\n没有收到 start 事件，无法测量")
        return 3

    pre = [r for ts, r, n in pk if ev_t - 1.0 <= ts < ev_t]
    base = sorted(pre)[len(pre)//2] if pre else 0.0
    thr = max(args.threshold, base * 3.0)
    print("\n基线 RMS(中位)=%.0f  阈值=%.0f  packets=%d" % (base, thr, len(pk)))

    post = [(ts, r) for ts, r, n in pk if ts >= ev_t]
    onset = None
    for i in range(len(post) - 1):
        if post[i][1] > thr and post[i + 1][1] > thr * 0.5:
            onset = post[i][0]
            break

    if onset is None:
        print("\n未检测到明显出声（检查音量/环境/MUSIC 配置）")
        return 4

    print("\n=== 结果 ===")
    print("  start 事件: %+.3fs" % (ev_t - start))
    print("  实际出声  : %+.3fs" % (onset - start))
    print("  事件->出声: %.3f 秒" % (onset - ev_t))
    print("\n  出声前后 RMS:")
    idx = next(i for i, (ts, r) in enumerate(post) if ts == onset)
    for ts, r in post[max(0, idx - 5): idx + 10]:
        print("    +%.2fs  RMS=%.0f" % (ts - ev_t, r))
    return 0


if __name__ == "__main__":
    sys.exit(main())
