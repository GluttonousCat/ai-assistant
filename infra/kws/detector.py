# -*- coding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

from typing import Optional
import numpy as np
import sherpa_onnx

from .utils import get_logger, check_file_exists

logger = get_logger(__name__)


class DetectionResult:
    def __init__(self, text: str, timestamp: float):
        self.text = text
        self.timestamp = timestamp

    def __str__(self) -> str:
        return self.text


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
        self._stream: Optional[sherpa_onnx.KeywordStream] = None

    def validate_files(self) -> bool:
        # Modified: Translated file descriptions
        files_to_check = [
            (self.tokens, "tokens file"),
            (self.encoder, "encoder model"),
            (self.decoder, "decoder model"),
            (self.joiner, "joiner model"),
            (self.keywords_file, "keywords file"),
        ]

        all_exist = True
        for filepath, description in files_to_check:
            if not check_file_exists(filepath, description):
                all_exist = False

        return all_exist

    def create_spotter(self) -> None:
        # Modified: Initialization logs
        logger.info("Initializing Keyword Spotter (sherpa-onnx)...")
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
        logger.info("Keyword Spotter initialization complete!")

    def create_stream(self) -> None:
        if self._kws is None:
            # Modified: Error message
            raise RuntimeError("Spotter not initialized. Please call create_spotter() first.")
        self._stream = self._kws.create_stream()
        logger.info("Detection stream created.")

    def is_ready(self) -> bool:
        if self._kws is None or self._stream is None:
            return False
        return self._kws.is_ready(self._stream)

    def accept_waveform(self, sample_rate: int, samples_float32: np.ndarray):
        if self._stream is None:
            # Modified: Error message
            raise RuntimeError("Detection stream not created. Please call create_stream() first.")
        self._stream.accept_waveform(sample_rate, samples_float32)

    def detect(self) -> Optional[str]:
        if self._kws is None or self._stream is None:
            # Modified: Error message
            raise RuntimeError("Spotter or stream not initialized.")

        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            result = self._kws.get_result(self._stream)

            if result:
                # Modified: Detection log
                logger.debug(f"Keyword detected: {result}")
                return result

        return None

    def reset_stream(self) -> None:
        if self._kws is None or self._stream is None:
            # Modified: Error message
            raise RuntimeError("Spotter or stream not initialized.")
        self._kws.reset_stream(self._stream)
        logger.debug("Detection stream reset.")