# -*- coding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import logging
from typing import Optional, Tuple
import numpy as np
import pyaudio

logger = logging.getLogger(__name__)


class AudioRecorder:
    def __init__(
            self,
            sample_rate: int = 16000, chunk_duration: float = 0.1,
            input_device_index: Optional[int] = None
    ):
        self.sample_rate = sample_rate
        self.chunk_duration = chunk_duration
        self.chunk_size = int(sample_rate * chunk_duration)
        self.input_device_index = input_device_index
        self._pyaudio: Optional[pyaudio.PyAudio] = None
        self._stream: Optional[pyaudio.Stream] = None

    def list_devices(self):
        p = pyaudio.PyAudio()
        devices = []
        try:
            default_input = p.get_default_input_device_info()
            # Modified: Default device info log
            logger.info(
                f"Default input device ID: {default_input['index']}, "
                f"Name: {default_input['name']}"
            )

            for i in range(p.get_device_count()):
                dev_info = p.get_device_info_by_index(i)
                if dev_info['maxInputChannels'] > 0:
                    devices.append({
                        'id': i,
                        'name': dev_info['name'],
                        'max_input_channels': dev_info['maxInputChannels']
                    })
                    # Modified: Device list log
                    logger.info(
                        f"  Device ID: {i}, Name: {dev_info['name']}, "
                        f"Input Channels: {dev_info['maxInputChannels']}")
        finally:
            p.terminate()

        return {
            'default_id': default_input['index'],
            'devices': devices
        }

    def start(self, device_index: Optional[int] = None) -> None:
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
        # Modified: Stream started log
        logger.info(
            f"Audio stream started: "
            f"Device ID={device_index}, "
            f"Sample Rate={self.sample_rate}, "
            f"Chunk Size={self.chunk_size}"
        )

    def read_chunk(self) -> Tuple[bytes, np.ndarray]:
        if self._stream is None:
            # Modified: Error message
            raise RuntimeError("Audio stream not started, please call start() first.")

        audio_bytes = self._stream.read(
            self.chunk_size, exception_on_overflow=False)
        samples_int16 = np.frombuffer(audio_bytes, dtype=np.int16)
        samples_float32 = samples_int16.astype(np.float32) / 32768.0

        return audio_bytes, samples_float32

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop_stream()
            self._stream.close()
            self._stream = None
        if self._pyaudio is not None:
            self._pyaudio.terminate()
            self._pyaudio = None
        # Modified: Stream stopped log
        logger.info("Audio stream stopped.")

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()