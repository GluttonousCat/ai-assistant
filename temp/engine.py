# -*- encoding: utf-8 -*-
from __future__ import annotations

import yaml

from openwakeword.model import Model
from logger import setup_logger

with open("config.yaml", "r") as f:
    config = yaml.safe_load(f)

logger = setup_logger(config)


class OWWEngine:
    def __init__(self, target_word="alexa", threshold=0.5):
        self.oww_model = Model(
            wakeword_models=[target_word],
            inference_framework="onnx"
        )
        self.threshold = threshold
        logger.info(f"OpenWakeWord 引擎初始化，阈值: {threshold}")

    def process(self, pcm_data):
        # openWakeWord 内部会自动做特征提取和推理
        prediction = self.oww_model.predict(pcm_data)

        # 获取当前词的置信度 (score)
        # prediction 是一个字典，如 {'alexa': 0.85}
        score = list(prediction.values())[0]

        if score > 0.1:
            logger.debug(f"唤醒得分: {score:.4f}")

        return score >= self.threshold

