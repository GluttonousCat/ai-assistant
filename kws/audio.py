#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
音频模块
处理音频录制和设备管理
"""

import logging
from typing import Optional, List, Dict, Any

import numpy as np
import pyaudio

from .utils import rms

logger = logging.getLogger(__name__)


class AudioRecorder:
    """音频录制器"""

    def __init__(self, sample_rate: int = 16000, chunk_duration: float = 0.1,
                 input_device_index: Optional[int] = None):
        """
        初始化音频录制器

        Args:
            sample_rate: 采样率 (Hz)
            chunk_duration: 每块时长 (秒)
            input_device_index: 输入设备 ID，None 为默认设备
        """
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self.chunk_size = int(sample_rate * chunk_duration)
        self.input_device_index = input_device_index
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None

    def list_devices(self) -> Dict[str, Any]:
        """列出所有可用的音频输入设备"""
        p = pyaudio.PyAudio()
        devices = []

        try:
            default_input = p.get_default_input_device_info()
            logger.info(f"默认输入设备 ID: {default_input['index']}, 名称：{default_input['name']}")

            for i in range(p.get_device_count()):
                dev_info = p.get_device_info_by_index(i)
                if dev_info['maxInputChannels'] > 0:
                    devices.append({
                        'id': i,
                        'name': dev_info['name'],
                        'max_input_channels': dev_info['maxInputChannels']
                    })
                    logger.info(f"  设备 ID: {i}, 名称：{dev_info['name']}, "
                               f"输入通道数：{dev_info['maxInputChannels']}")
        finally:
            p.terminate()

        return {
            'default_id': default_input['index'],
            'devices': devices
        }

    def start(self, device_index: Optional[int] = None) -> None:
        """
        开始录制

        Args:
            device_index: 设备 ID，None 使用默认
        """
        if device_index is None:
            device_index = self.input_device_index

        self._pyaudio = pyaudio.PyAudio()
        self._stream = self._pyaudio.open(
            format=pyaudio.paInt16,
            channels=1,
            rate=self.sample_rate,
            input=True,
            input_device_index=device_index,
            frames_per_buffer=self.chunk_size,
        )
        logger.info(f"音频流已启动：设备 ID={device_index}, "
                   f"采样率={self.sample_rate}, 块大小={self.chunk_size}")

    def read_chunk(self) -> np.ndarray:
        """
        读取一个音频块

        Returns:
            float32 数组，归一化到 [-1, 1]
        """
        if self._stream is None:
            raise RuntimeError("音频流未启动，请先调用 start()")

        audio_data = self._stream.read(self.chunk_size, exception_on_overflow=False)
        samples_int16 = np.frombuffer(audio_data, dtype=np.int16)
        return samples_int16.astype(np.float32) / 32768.0

    def read_raw_bytes(self) -> bytes:
        """
        读取原始 bytes 数据（用于保存 WAV）

        Returns:
            int16 原始 bytes
        """
        if self._stream is None:
            raise RuntimeError("音频流未启动，请先调用 start()")

        return self._stream.read(self.chunk_size, exception_on_overflow=False)

    def stop(self) -> None:
        """停止录制并释放资源"""
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pyaudio is not None:
            self._pyaudio.terminate()
            self._pyaudio = None
        logger.info("音频流已停止")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
