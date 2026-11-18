#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""独立进程：让 G1 播报「小野AI已启动」。

由 web_arm_control 在 start_xiaoye 成功时以子进程拉起。
独立进程有自己的 DDS 通道，避免与 Flask 进程内的音频客户端并发而 SEGV。
"""
import sys
import time

sys.path.append('/home/unitree/tang/WK/unitree_sdk2_python')

from unitree_sdk2py.core.channel import ChannelFactoryInitialize  # noqa: E402
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient  # noqa: E402

TEXT = '小野AI已启动'


def main():
    ChannelFactoryInitialize(0, 'eth0')
    client = AudioClient()
    client.Init()
    client.SetTimeout(10.0)
    try:
        client.SetVolume(100)
    except Exception:
        pass
    client.TtsMaker(TEXT, 0)
    time.sleep(2.0)
    print('ANNOUNCED', TEXT)


if __name__ == '__main__':
    main()
