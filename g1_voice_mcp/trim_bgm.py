#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""裁掉 BGM 缓存开头的轻柔/静音前奏，生成 <state>_<start>s.wav。

只新增文件，不修改原始 _0.0s.wav。
"""
import wave

CACHE = "/home/unitree/huang/unitree-app-templates/G1_joystick/music/cache"
SR = 16000

TRIMS = [
    ("Video22", 2.5),
    ("Video25", 2.5),
    ("Video27", 1.0),
]


def trim(state, seconds):
    src = "%s/%s_0.0s.wav" % (CACHE, state)
    dst = "%s/%s_%.1fs.wav" % (CACHE, state, seconds)
    with wave.open(src, "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SR:
            raise SystemExit("%s 格式不符: ch=%d sw=%d sr=%d" % (
                src, w.getnchannels(), w.getsampwidth(), w.getframerate()))
        total = w.getnframes()
        w.setpos(int(seconds * SR))
        frames = w.readframes(total - int(seconds * SR))
    with wave.open(dst, "wb") as o:
        o.setnchannels(1)
        o.setsampwidth(2)
        o.setframerate(SR)
        o.writeframes(frames)
    print("%s -> %.1f s, 剩余 %.1f s" % (dst, seconds, len(frames) / 2.0 / SR))


for state, sec in TRIMS:
    trim(state, sec)
