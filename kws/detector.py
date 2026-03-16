#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, List

import numpy as np

try:
    import sherpa_onnx
except ImportError:
    raise ImportError("请先安装 sherpa-onnx: pip3 install sherpa-onnx")

from .utils import check_file_exists

logger = logging.getLogger(__name__)


@dataclass
class DetectionResult:
    keyword: str
    timestamp: str
    count: int


class KeywordDetector:
    def __init__(
        self,
        tokens: str,
        encoder: str,
        decoder: str,
        joiner: str,
        keywords_file: str,
        num_threads: int = 2,
        provider: str = "cpu",
        max_active_paths: int = 4,
        num_trailing_blanks: int = 1,
        keywords_score: float = 1.5,
        keywords_threshold: float = 0.07,
    ):
        """
        初始化关键词检测器

        Args:
            tokens: tokens.txt 路径
            encoder: encoder ONNX 模型路径
            decoder: decoder ONNX 模型路径
            joiner: joiner ONNX 模型路径
            keywords_file: 关键词文件路径
            num_threads: 推理线程数
            provider: 推理后端 (cpu/cuda/coreml)
            max_active_paths: 解码时保留的最大活跃路径数
            num_trailing_blanks: 关键词后跟随的空白帧数
            keywords_score: 关键词 token 的增强分数
            keywords_threshold: 关键词触发阈值
        """
        self.tokens = tokens
        self.encoder = encoder
        self.decoder = decoder
        self.joiner = joiner
        self.keywords_file = keywords_file
        self.num_threads = num_threads
        self.provider = provider
        self.max_active_paths = max_active_paths
        self.num_trailing_blanks = num_trailing_blanks
        self.keywords_score = keywords_score
        self.keywords_threshold = keywords_threshold

        self._kws: Optional[sherpa_onnx.KeywordSpotter] = None
        self._stream = None

    def validate_files(self) -> bool:
        """验证所有模型文件是否存在"""
        files_ok = True
        files_ok &= check_file_exists(self.tokens, "tokens 文件")
        files_ok &= check_file_exists(self.encoder, "encoder 模型")
        files_ok &= check_file_exists(self.decoder, "decoder 模型")
        files_ok &= check_file_exists(self.joiner, "joiner 模型")
        files_ok &= check_file_exists(self.keywords_file, "关键词文件")
        return files_ok

    def create_spotter(self) -> None:
        """创建关键词检测器"""
        logger.info("正在初始化关键词检测器...")

        self._kws = sherpa_onnx.KeywordSpotter(
            tokens=self.tokens,
            encoder=self.encoder,
            decoder=self.decoder,
            joiner=self.joiner,
            num_threads=self.num_threads,
            max_active_paths=self.max_active_paths,
            keywords_file=self.keywords_file,
            keywords_score=self.keywords_score,
            keywords_threshold=self.keywords_threshold,
            num_trailing_blanks=self.num_trailing_blanks,
            provider=self.provider,
        )

        logger.info("关键词检测器初始化完成！")

    def create_stream(self) -> None:
        """创建检测流"""
        if self._kws is None:
            raise RuntimeError("检测器未初始化，请先调用 create_spotter()")
        self._stream = self._kws.create_stream()

    def reset_stream(self) -> None:
        """重置检测流"""
        if self._kws is not None and self._stream is not None:
            self._kws.reset_stream(self._stream)

    def accept_waveform(self, sample_rate: int, samples: np.ndarray) -> None:
        """
        输入音频波形

        Args:
            sample_rate: 采样率
            samples: float32 音频样本
        """
        if self._stream is None:
            raise RuntimeError("流未创建，请先调用 create_stream()")
        self._stream.accept_waveform(sample_rate, samples)

    def detect(self) -> Optional[str]:
        """
        检测关键词

        Returns:
            检测到的关键词，无结果返回 None
        """
        if self._kws is None or self._stream is None:
            return None

        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            result = self._kws.get_result(self._stream)
            if result:
                return result
        return None

    def is_ready(self) -> bool:
        """检测器是否就绪"""
        if self._kws is None or self._stream is None:
            return False
        return self._kws.is_ready(self._stream)


class DetectionState:
    """检测状态管理器"""

    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_duration: float = 0.1,
        pre_roll_seconds: float = 0.5,
        silence_rms_threshold: float = 0.015,
        silence_count_threshold: int = 15,
        min_record_seconds: float = 1.5,
        max_record_seconds: float = 15.0,
        post_wake_grace_seconds: float = 1.2,
    ):
        """
        初始化检测状态

        Args:
            sample_rate: 采样率
            chunk_duration: 每块时长
            pre_roll_seconds: 预滚动缓冲时长
            silence_rms_threshold: 静音 RMS 阈值
            silence_count_threshold: 静音判定块数阈值
            min_record_seconds: 最小录音时长
            max_record_seconds: 最大录音时长
            post_wake_grace_seconds: 唤醒后宽限时长
        """
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self.chunk_size = int(sample_rate * chunk_duration)

        # 从环境变量读取参数，支持动态调整
        self.pre_roll_seconds = float(os.getenv("PRE_ROLL_SECONDS", str(pre_roll_seconds)))
        self.pre_roll_chunks = max(1, int(self.pre_roll_seconds / chunk_duration))

        self.silence_rms_threshold = float(os.getenv("SILENCE_RMS_THRESHOLD", str(silence_rms_threshold)))
        self.silence_count_threshold = int(os.getenv("SILENCE_COUNT_THRESHOLD", str(silence_count_threshold)))

        self.min_record_seconds = float(os.getenv("MIN_RECORD_SECONDS", str(min_record_seconds)))
        self.max_record_seconds = float(os.getenv("MAX_RECORD_SECONDS", str(max_record_seconds)))

        self.min_record_chunks = max(1, int(self.min_record_seconds / chunk_duration))
        self.max_record_chunks = max(1, int(self.max_record_seconds / chunk_duration))

        self.post_wake_grace_seconds = float(os.getenv("POST_WAKE_GRACE_SECONDS", str(post_wake_grace_seconds)))

        # 状态变量
        self.state = "PASSIVE"  # PASSIVE | ACTIVE
        self.pre_roll: List[bytes] = []
        self.recorded_frames: List[bytes] = []
        self.silent_count = 0
        self.chunk_count = 0
        self.active_start_time = 0.0
        self.heard_speech = False
        self.detection_count = 0

    def add_to_pre_roll(self, audio_data: bytes) -> None:
        """添加到预滚动缓冲"""
        self.pre_roll.append(audio_data)
        if len(self.pre_roll) > self.pre_roll_chunks:
            self.pre_roll = self.pre_roll[-self.pre_roll_chunks:]

    def get_pre_roll(self) -> List[bytes]:
        """获取预滚动缓冲"""
        return list(self.pre_roll)

    def start_recording(self, pre_roll_data: List[bytes]) -> None:
        """开始录音"""
        self.recorded_frames = list(pre_roll_data)
        self.silent_count = 0
        self.chunk_count = 0
        self.active_start_time = time.time()
        self.heard_speech = False
        self.state = "ACTIVE"

    def update_silence(self, is_silent: bool) -> None:
        """更新静音计数"""
        if is_silent:
            self.silent_count += 1
        else:
            self.silent_count = 0
            self.heard_speech = True

    def should_stop_recording(self) -> bool:
        """判断是否应该停止录音"""
        if self.chunk_count >= self.max_record_chunks:
            return True

        elapsed = time.time() - self.active_start_time
        if (elapsed >= self.post_wake_grace_seconds
                and self.chunk_count >= self.min_record_chunks
                and self.heard_speech
                and self.silent_count > self.silence_count_threshold):
            return True

        return False

    def stop_recording(self) -> None:
        """停止录音，重置状态"""
        self.recorded_frames = []
        self.silent_count = 0
        self.chunk_count = 0
        self.active_start_time = 0.0
        self.heard_speech = False
        self.state = "PASSIVE"
        self.pre_roll = []

    def increment_detection_count(self) -> int:
        """增加检测计数并返回新值"""
        self.detection_count += 1
        return self.detection_count

    def get_timestamp(self) -> str:
        """获取当前时间戳"""
        return datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]

    def get_recording_filename(self, base_dir: str) -> str:
        """生成录音文件名"""
        import os
        os.makedirs(os.path.join(base_dir, "recording"), exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
        return os.path.join(base_dir, "recording", f"recording_{timestamp}.wav")
