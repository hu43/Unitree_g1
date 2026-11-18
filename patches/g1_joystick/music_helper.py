#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
G1_joystick 音乐伴随进程（BGM 同步播放）

订阅 rt/g1joystick/event (std_msgs String_)，与 C++ 控制器状态机同步：
  start:<状态名>  -> 播放该状态对应的 BGM
  stop:<状态名>   -> 停止播放（动作超时/双击Start打断/姿态保护退出都会触发）

用法: python3 music_helper.py [net_interface]   (默认 lo)
部署说明: 优先加载项目自带 vendor/（unitree_sdk2py + cyclonedds），目标机无需 pip 安装；
          BGM 已预转换为 music/cache/*.wav（16kHz mono s16le），目标机无需 ffmpeg。
"""
import os
import sys
import time
import threading

BASE = os.path.dirname(os.path.abspath(__file__))      # .../G1_joystick/music
PROJ = os.path.dirname(BASE)                            # .../G1_joystick
VENDOR = os.path.join(PROJ, "vendor")
if os.path.isdir(VENDOR):
    sys.path.insert(0, VENDOR)                          # 免安装：使用项目自带的 python 包

from unitree_sdk2py.core.channel import (
    ChannelFactoryInitialize,
    ChannelFactory,
    ChannelSubscriber,
)
from unitree_sdk2py.idl.std_msgs.msg.dds_ import String_
from unitree_sdk2py.g1.audio.g1_audio_client import AudioClient
from cyclonedds.qos import Qos, Policy

NET = sys.argv[1] if len(sys.argv) > 1 else "eth0"
VOLUME = 90
APP_NAME = "g1joystick"
CHUNK = 96000        # 字节；16kHz mono s16le = 32000 B/s，即每块 3 秒
SEND_INTERVAL = 1.0  # 每 1s 发 3s 数据 -> 3 倍速灌喇叭缓冲（与官方示例一致）

# 状态名 -> BGM 配置（file 相对 music/）
#   start    音乐起始秒数（对齐节拍）
#   delay    动作开始后延迟多少秒才起音乐（动作先行）；缺省 0 = 与动作同时起
#            （BGM 提前量则相反，由 config.yaml 的 FSM.<状态>.music_lead 在 C++ 侧控制）
#   duration 截取的音乐时长（秒）：delay + duration == 动作时长 -> 音乐与动作同时自然结束
MUSIC = {
    "Gangnam": {"file": "何鹏&PSY+-+江南Style.mp3", "start": 0.0},
    "Video22": {"file": "kungfu_video22_fix.mp3",   "start": 2.5},  # 跳过开头 2.5s 轻柔前奏
    # video25 广播体操动作(91.6s)：BGM 已换成功夫曲（不再用广播体操曲），
    # 先播 4s 再开始动作（config.yaml music_lead: 4.0），动作结束时随 stop 事件停
    "Video25": {"file": "kungfu_video22_fix.mp3", "start": 2.5},  # 跳过开头 2.5s 轻柔前奏
    # video27_fix(28.2s)：BGM 先播 4s（config.yaml music_lead: 4.0），动作结束时随 stop 事件停
    "Video27": {"file": "Scott Buckley - Wanderlust.mp3", "start": 1.0},  # 跳过 1.0s 静音
    # Kungfu3(92.8s)：男儿当自强，BGM 先播 4s（config.yaml music_lead: 4.0）
    "Kungfu3": {"file": "男儿当自强-林子祥.mp3", "start": 0.0},
    # Jixiewu1 机械舞1(20.6s)：HOZIN，BGM 先播 4s（config.yaml music_lead: 4.0）
    "Jixiewu1": {"file": "HOZIN与SIM（机械舞） 99-176435(HOZIN_爱给网_aigei_com.mp3", "start": 0.0},
    # Jixiewu2 机械舞2(12.4s)：嘻哈异想霓虹迪斯科，BGM 先播 4s（config.yaml music_lead: 4.0）
    "Jixiewu2": {"file": "机械舞蹈-嘻哈异想-霓虹迪斯科_爱给网_aigei_com.mp3", "start": 0.0},
    # Su(5.07s)：SIUUU音效（源 4.6s，cache/Su_0.0s.wav 已用 apad 静音补齐到 5.07s），
    # 无 music_lead，与动作同时起同时停
    "Su": {"file": "SIUUU音效 siu音效(SIUUU SOUND EFFE_爱给网_aigei_com.mp3", "start": 0.0},
}


# ---------------- WAV 读取 / 预转换 ----------------
def read_wav_pcm(path):
    """读取 16kHz/mono/s16le 的 WAV，返回 PCM bytes；格式不符返回 None。"""
    try:
        with open(path, "rb") as f:
            def rd(n):
                return f.read(n)
            import struct
            if rd(4) != b"RIFF":
                return None
            rd(4)
            if rd(4) != b"WAVE":
                return None
            fmt = None
            while True:
                cid = rd(4)
                if len(cid) < 4:
                    return None
                (csz,) = struct.unpack("<I", rd(4))
                if cid == b"fmt ":
                    fmt = struct.unpack("<HHIIHH", rd(16))
                    if csz > 16:
                        rd(csz - 16)
                elif cid == b"data":
                    return rd(csz)
                else:
                    rd(csz)
    except Exception as e:
        print("[wav] read error: %s" % e)
        return None


def ensure_wav(state):
    """返回状态对应的 16k wav 路径；cache 缺失时用 ffmpeg 现场转换（一般只在换歌时走到）。"""
    cfg = MUSIC[state]
    cache_dir = os.path.join(BASE, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    duration = cfg.get("duration")
    if duration is not None:
        dst = os.path.join(cache_dir, "%s_%.1fs_%.1fs.wav" % (state, cfg["start"], duration))
    else:
        dst = os.path.join(cache_dir, "%s_%.1fs.wav" % (state, cfg["start"]))
    if os.path.exists(dst) and os.path.getsize(dst) > 44:
        return dst
    src = os.path.join(BASE, cfg["file"])
    import subprocess
    cmd = ["ffmpeg", "-y", "-ss", str(cfg["start"]), "-i", src]
    if duration is not None:
        cmd += ["-t", str(duration)]                       # 截取时长：音乐与动作同时结束
    cmd += ["-ac", "1", "-ar", "16000", "-sample_fmt", "s16", dst]
    print("[ffmpeg] %s" % " ".join(cmd))
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        print("[ffmpeg ERROR] %s" % r.stderr[-500:])
        return None
    return dst


# ---------------- 播放器 ----------------
class Player:
    def __init__(self):
        self.ac = AudioClient()
        self.ac.SetTimeout(10.0)
        self.ac.Init()
        self.ac.SetVolume(VOLUME)
        self.lock = threading.Lock()
        self.stop_evt = None
        self.thread = None

    def play(self, state):
        wav = ensure_wav(state)
        if not wav:
            return
        pcm = read_wav_pcm(wav)
        if not pcm:
            print("[music] bad wav: %s" % wav)
            return
        print("[music] play %s (%.1fs), volume=%d" % (state, len(pcm) / 32000.0, VOLUME))
        self.stop()
        with self.lock:
            self.stop_evt = threading.Event()
            evt = self.stop_evt
        delay = MUSIC[state].get("delay", 0.0)
        self.thread = threading.Thread(
            target=self._stream, args=(state, pcm, evt, delay), daemon=True)
        self.thread.start()

    def _stream(self, state, pcm, evt, delay=0.0):
        """按 3 倍速把 PCM 推给喇叭；自然播完后 drain 并停止。"""
        try:
            # 动作先行模式：先等 delay 秒再起音乐（期间收到 stop 立即放弃）
            if delay > 0:
                print("[music] %s action-first, BGM starts in %.1fs" % (state, delay))
                if evt.wait(delay):
                    return
            t0 = time.time()
            for i in range(0, len(pcm), CHUNK):
                if evt.is_set():
                    return
                self.ac.PlayStream(APP_NAME, "bgm", pcm[i:i + CHUNK])
                time.sleep(SEND_INTERVAL)
            # 发送完比播放快，等实时播放追平后再 PlayStop，避免切尾
            audio_dur = len(pcm) / 32000.0
            sent_time = time.time() - t0
            remain = audio_dur - sent_time + 0.5
            if remain > 0:
                evt.wait(remain)
            if not evt.is_set():
                self.ac.PlayStop(APP_NAME)
                print("[music] %s finished" % state)
        except Exception as e:
            print("[music] stream error: %s" % e)

    def stop(self):
        with self.lock:
            if self.stop_evt:
                self.stop_evt.set()
        if self.thread:
            self.thread.join(timeout=2.0)
            self.thread = None
        try:
            self.ac.PlayStop(APP_NAME)
        except Exception:
            pass


def main():
    ChannelFactoryInitialize(0, NET)
    player = Player()

    # 预热：确认各 BGM 的 wav 就绪（首次换歌时才需要 ffmpeg）
    for s in MUSIC:
        ensure_wav(s)

    def handler(msg):
        try:
            text = getattr(msg, "data", "") or ""
            if text.startswith("start:"):
                state = text.split(":", 1)[1]
                if state in MUSIC:
                    player.play(state)
                else:
                    print("[music] %s 无BGM配置，忽略" % state)
            elif text.startswith("stop:"):
                print("[music] stop (%s)" % text)
                player.stop()
        except Exception as e:
            print("[music] handler error: %s" % e)

    # 去重：C++ 侧每个事件连发 3 帧，这里对 1 秒内重复的同名事件只处理一次。
    _last = {"text": None, "t": 0.0}

    def handle_event(msg):
        text = getattr(msg, "data", "") or ""
        now = time.time()
        if text == _last["text"] and (now - _last["t"]) < 1.0:
            return
        _last["text"] = text
        _last["t"] = now
        handler(msg)

    # 轮询读取（而不是回调监听）：实测回调方式在动作执行期间会丢事件
    # （5 次单帧 start 只收到 3 次、stop 全部丢失）。KEEP_LAST(20) 保证
    # 处理当前事件期间到达的重复帧不会互相覆盖。
    factory = ChannelFactory()
    channel = factory.CreateChannel("rt/g1joystick/event", String_)
    channel.SetReader(Qos(Policy.History.KeepLast(20)), None, 0)

    print("[music] helper ready on %s, topic rt/g1joystick/event (polling), states=%s"
          % (NET, list(MUSIC)))
    while True:
        try:
            msg = channel.Read(None)
        except Exception as e:
            print("[music] read error: %s" % e)
            time.sleep(0.5)
            continue
        if msg is not None:
            handle_event(msg)


if __name__ == "__main__":
    main()
