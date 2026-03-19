# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
import numpy as np
import sherpa_onnx

logger = logging.getLogger(__name__)


class Detector:
    def __init__(self, args):
        logger.info("正在初始化关键词检测器 (sherpa-onnx)...")
        self.kws = sherpa_onnx.KeywordSpotter(
            tokens=args.tokens,
            encoder=args.encoder,
            decoder=args.decoder,
            joiner=args.joiner,
            num_threads=args.num_threads,
            max_active_paths=args.max_active_paths,
            keywords_file=args.keywords_file,
            keywords_score=args.keywords_score,
            keywords_threshold=args.keywords_threshold,
            num_trailing_blanks=args.num_trailing_blanks,
            provider=args.provider,
        )

        self.stream = self.kws.create_stream()
        logger.info("关键词检测器初始化完成！")

    def accept_waveform(self, sample_rate: int, samples_float32: np.ndarray):
        """
        向检测流中喂入音频数据
        :param sample_rate: 音频采样率 (如 16000)
        :param samples_float32: 经过归一化的一维 float32 numpy 数组 (-1.0 ~ 1.0)
        """
        self.stream.accept_waveform(sample_rate, samples_float32)

    def detect(self) -> tuple[bool, str | None]:
        """
        消费流中所有就绪的数据并检测关键词
        :return: (是否触发, 关键词结果字符串)
        """
        triggered = False
        detected_result = None

        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            result = self.kws.get_result(self.stream)

            if result:
                triggered = True
                detected_result = result
                break

        return triggered, detected_result

    def reset_stream(self):
        self.kws.reset_stream(self.stream)