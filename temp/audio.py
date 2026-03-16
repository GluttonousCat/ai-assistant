# -*- encoding: utf-8 -*-
from __future__ import annotations

import logging
import queue
import time

import sounddevice as sd
import webrtcvad

logger = logging.getLogger(__name__)


class AudioStreamer:
    def __init__(self, sample_rate=16000, frame_duration_ms=30):
        self.sample_rate = sample_rate
        self.frame_size = int(self.sample_rate * frame_duration_ms / 1000)
        self.vad = webrtcvad.Vad(3)

        self.queue = queue.Queue(maxsize=20)
        self.is_running = False

    def audio_callback(self, in_data, status):
        if status:
            logger.warning(f"Audio callback status: {status}")

        try:
            self.queue.put_nowait(in_data.copy())
        except queue.Full:
            logger.debug("Queue full, dropping frame")

    def run(self):
        self.is_running = True
        logger.info("音频采集流开始运行...")

        while self.is_running:
            try:
                with sd.InputStream(
                        samplerate=self.sample_rate,
                        channels=1,
                        dtype='int16',
                        blocksize=self.frame_size,
                        callback=self.audio_callback
                ):

                    logger.info("声卡设备初始化成功")

                    while self.is_running:
                        frame = self.queue.get(timeout=1.0)

                        if self.vad.is_speech(
                                frame.tobytes(), self.sample_rate):
                            # TODO: 唤醒引擎
                            logger.debug("检测到人声")

            except sd.PortAudioError as e:
                logger.error(f"音频设备异常: {e}，5秒后尝试重连...")
                time.sleep(5)
            except Exception as e:
                logger.critical(f"未知错误: {e}")
                self.stop()

    def stop(self):
        self.is_running = False