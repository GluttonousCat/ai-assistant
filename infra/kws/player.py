# -*- coding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import logging
from typing import Optional
import numpy as np
import pyaudio

logger = logging.getLogger(__name__)


class AudioPlayer:
    def __init__(self, sample_rate: int = 16000):
        self.sample_rate = sample_rate
        self._pyaudio: Optional[pyaudio.PyAudio] = None

    def play_tone(
        self,
        freq_hz: float = 880.0,
        duration_ms: float = 120.0,
        volume: float = 0.25,
    ) -> None:
        try:
            self._pyaudio = pyaudio.PyAudio()
            duration_sec = duration_ms / 1000.0
            num_samples = int(self.sample_rate * duration_sec)

            # Generate sine wave
            t = np.linspace(0, duration_sec, num_samples, False)
            samples = np.sin(2 * np.pi * freq_hz * t) * volume

            # Convert to int16
            samples_int16 = (samples * 32767).astype(np.int16)

            # Playback
            stream = self._pyaudio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=self.sample_rate,
                output=True,
            )
            stream.write(samples_int16.tobytes())
            stream.stop_stream()
            stream.close()

            # Modified log
            logger.debug(f"Tone playback finished: {freq_hz}Hz, {duration_ms}ms")

        except Exception as e:
            # Modified error log
            logger.error(f"Failed to play tone: {e}")
        finally:
            if self._pyaudio is not None:
                self._pyaudio.terminate()
                self._pyaudio = None

    def play_file(self, file_path: str) -> bool:
        try:
            import wave

            with wave.open(file_path, 'rb') as wav_file:
                self._pyaudio = pyaudio.PyAudio()

                # Read WAV file parameters
                n_channels = wav_file.getnchannels()
                sampwidth = wav_file.getsampwidth()
                framerate = wav_file.getframerate()
                n_frames = wav_file.getnframes()

                # Open output stream
                stream = self._pyaudio.open(
                    format=self._pyaudio.get_format_from_width(sampwidth),
                    channels=n_channels,
                    rate=framerate,
                    output=True,
                )

                # Read and play all frames
                data = wav_file.readframes(n_frames)
                stream.write(data)

                stream.stop_stream()
                stream.close()

                # Modified log
                logger.info(f"WAV file playback finished: {file_path}")
                return True

        except Exception as e:
            # Modified error log
            logger.error(f"Failed to play WAV file: {e}")
            return False
        finally:
            if self._pyaudio is not None:
                self._pyaudio.terminate()
                self._pyaudio = None