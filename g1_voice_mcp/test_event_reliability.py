#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""事件可靠性测试：向 rt/g1joystick/event 单帧发 start/stop，统计 helper 是否播放。

只播放音乐，不动机器人。用法：
  python3.8 test_event_reliability.py --rounds 5 --dance Su
"""
import argparse
import sys
import time

from unitree_sdk2py.core.channel import ChannelFactoryInitialize, ChannelPublisher
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--dance", default="Su")
    ap.add_argument("--play-secs", type=float, default=6.0)
    ap.add_argument("--iface", default="eth0")
    args = ap.parse_args()

    ChannelFactoryInitialize(0, args.iface)
    pub = ChannelPublisher("rt/g1joystick/event", String_)
    pub.Init()

    for i in range(1, args.rounds + 1):
        ts = time.strftime("%H:%M:%S")
        print("[%d] %s  start:%s" % (i, ts, args.dance))
        pub.Write(String_(data="start:" + args.dance))
        time.sleep(args.play_secs)
        ts = time.strftime("%H:%M:%S")
        print("[%d] %s  stop:%s" % (i, ts, args.dance))
        pub.Write(String_(data="stop:" + args.dance))
        time.sleep(1.5)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
