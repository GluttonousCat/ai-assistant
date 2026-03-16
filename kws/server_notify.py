#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
服务器通知模块
处理与 WebSocket 服务器的通信和本地音频播放
"""

import logging
import os
import threading
import time
from typing import Optional

import numpy as np
import pyaudio
import requests

logger = logging.getLogger(__name__)

SERVER_BASE_URL = os.getenv("KWS_SERVER_URL", "http://localhost:9897")


class ServerNotifier:
    """服务器通知器"""

    def __init__(self, base_url: str = SERVER_BASE_URL):
        self.base_url = base_url
        self._session = requests.Session()

    def interrupt_play(self) -> None:
        """通知服务器中断当前播放"""
        try:
            self._session.get(f"{self.base_url}/interrupt_play", timeout=0.5)
        except Exception:
            pass

    def to_upload(self, filename: str) -> None:
        """通知服务器上传文件"""
        try:
            abs_path = os.path.abspath(filename)
            logger.info(f"📤 通知服务器上传文件：{abs_path}")
            self._session.get(
                f"{self.base_url}/do_send",
                params={"fname": abs_path},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Failed to notify server to upload: {e}")

    def play_preset(self) -> float:
        """播放预设回应语，返回时长"""
        try:
            resp = self._session.get(f"{self.base_url}/play_preset", timeout=5)
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            duration = float(data.get("duration") or 0.0)
            return max(0.0, duration)
        except Exception:
            return 0.0

    def kws_ready(self) -> None:
        """KWS 就绪时发送提示"""
        try:
            params = {}
            sound_file = os.environ.get("KWS_READY_SOUND_FILE", "").strip()
            if sound_file:
                params["file"] = sound_file
            style = os.environ.get("KWS_READY_SOUND_STYLE", "").strip()
            if style:
                params["style"] = style
            vol = os.environ.get("KWS_READY_SOUND_VOL", "").strip()
            if vol:
                params["vol"] = vol
            self._session.get(f"{self.base_url}/beep_ready", params=params, timeout=0.5)
        except Exception:
            pass

    def close(self) -> None:
        """关闭 session"""
        self._session.close()


class AudioPlayer:
    """音频播放器，用于播放提示音"""

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    def play_tone(self, freq_hz: float = 880.0, duration_ms: int = 150,
                  volume: float = 0.3, blocking: bool = False) -> None:
        """
        播放提示音

        Args:
            freq_hz: 频率 (Hz)
            duration_ms: 时长 (ms)
            volume: 音量 (0.0-1.0)
            blocking: 是否阻塞等待播放完成
        """
        def _play():
            try:
                sr = self.sample_rate
                n_samples = int(sr * duration_ms / 1000)

                # 生成正弦波
                t = np.linspace(0, duration_ms / 1000, n_samples, dtype=np.float32)
                tone = np.sin(2 * np.pi * freq_hz * t) * volume

                # 淡入淡出，避免爆音
                ramp = min(int(sr * 0.01), n_samples // 10)
                if ramp > 1:
                    w = np.linspace(0.0, 1.0, ramp, dtype=np.float32)
                    tone[:ramp] *= w
                    tone[-ramp:] *= w[::-1]

                # 播放
                p = pyaudio.PyAudio()
                stream = p.open(format=pyaudio.paFloat32, channels=1, rate=sr,
                               output=True)
                stream.write(tone.tobytes())
                stream.stop_stream()
                stream.close()
                p.terminate()
            except Exception as e:
                logger.debug(f"播放提示音失败：{e}")

        if blocking:
            _play()
        else:
            threading.Thread(target=_play, daemon=True).start()

    def play_error_tone(self, blocking: bool = False) -> None:
        """
        播放错误提示音（连续两声"滴滴"）

        Args:
            blocking: 是否阻塞等待播放完成
        """
        def _play():
            try:
                sr = self.sample_rate
                p = pyaudio.PyAudio()
                stream = p.open(format=pyaudio.paFloat32, channels=1, rate=sr,
                               output=True)

                for freq in [440, 440]:
                    n_samples = int(sr * 0.15)
                    t = np.linspace(0, 0.15, n_samples, dtype=np.float32)
                    tone = np.sin(2 * np.pi * freq * t) * 0.3

                    # 淡入淡出
                    ramp = n_samples // 10
                    w = np.linspace(0.0, 1.0, ramp, dtype=np.float32)
                    tone[:ramp] *= w
                    tone[-ramp:] *= w[::-1]

                    stream.write(tone.tobytes())
                    time.sleep(0.15)  # 两声之间的间隔

                stream.stop_stream()
                stream.close()
                p.terminate()
            except Exception as e:
                logger.debug(f"播放错误提示音失败：{e}")

        if blocking:
            _play()
        else:
            threading.Thread(target=_play, daemon=True).start()
