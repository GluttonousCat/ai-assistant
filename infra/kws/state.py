# -*- coding: utf-8 -*-
"""
@file: state.py
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import os
import time
from typing import List, Optional
from datetime import datetime

from .utils import get_logger

logger = get_logger(__name__)


class DetectionState:
    def __init__(
        self,
        sample_rate: int = 16000,
        chunk_duration: float = 0.1,
        pre_roll_seconds: float = 1.0,
        silence_rms_threshold: float = 0.01,
        silence_count_threshold: int = 10,
        min_record_seconds: float = 1.0,
        max_record_seconds: float = 10.0,
        post_wake_grace_seconds: float = 0.5,
    ):
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self.chunk_size = int(sample_rate * chunk_duration)
        self.pre_roll_seconds = pre_roll_seconds
        self.silence_rms_threshold = silence_rms_threshold
        self.silence_count_threshold = silence_count_threshold
        self.min_record_seconds = min_record_seconds
        self.max_record_seconds = max_record_seconds
        self.post_wake_grace_seconds = post_wake_grace_seconds

        self._state = "PASSIVE"
        self._pre_roll_buffer: List[bytes] = []
        self._max_pre_roll_chunks = int(pre_roll_seconds / chunk_duration)

        self.recorded_frames: List[bytes] = []
        self.chunk_count = 0
        self._recording_start_time: Optional[float] = None

        self._silence_count = 0
        self._consecutive_silence = 0

        self.detection_count = 0
        self._last_detection_time: Optional[float] = None

    @property
    def state(self) -> str:
        return self._state

    def get_pre_roll(self) -> List[bytes]:
        return self._pre_roll_buffer.copy()

    def add_to_pre_roll(self, audio_bytes: bytes) -> None:
        self._pre_roll_buffer.append(audio_bytes)
        if len(self._pre_roll_buffer) > self._max_pre_roll_chunks:
            self._pre_roll_buffer.pop(0)

    def start_recording(self, pre_roll_frames: Optional[List[bytes]] = None) -> None:
        self._state = "ACTIVE"
        self.recorded_frames = []
        self.chunk_count = 0
        self._silence_count = 0
        self._consecutive_silence = 0
        self._recording_start_time = time.time()

        if pre_roll_frames:
            self.recorded_frames.extend(pre_roll_frames)
            self.chunk_count += len(pre_roll_frames)
        # Modified: Start recording log
        logger.info(f"🎙️ Recording started (Pre-roll frames: {len(self.recorded_frames)})")

    def stop_recording(self) -> None:
        self._state = "PASSIVE"
        self._pre_roll_buffer = []

    def update_silence(self, is_silent: bool) -> None:
        if is_silent:
            self._consecutive_silence += 1
            self._silence_count += 1
        else:
            self._consecutive_silence = 0

    def should_stop_recording(self) -> bool:
        if self._recording_start_time is None:
            return False

        elapsed = time.time() - self._recording_start_time

        if elapsed >= self.max_record_seconds:
            # Modified: Max duration protection log
            logger.info(f"⏳ Max recording duration reached: {elapsed:.2f}s")
            return True

        if elapsed < self.min_record_seconds:
            return False

        if self._consecutive_silence >= self.silence_count_threshold:
            # Modified: Silence detection log
            logger.info(f"🔇 Silence detected / End of speech (Consecutive silent frames: {self._consecutive_silence})")
            return True

        return False

    def get_recording_filename(self, top_dir: str) -> str:
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")[:-3]
        filename = f"recording_{timestamp}.wav"
        return os.path.join(top_dir, "recording", filename)

    def get_timestamp(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def increment_detection_count(self) -> int:
        self.detection_count += 1
        self._last_detection_time = time.time()
        return self.detection_count
