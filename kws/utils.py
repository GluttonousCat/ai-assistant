# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def check_file_exists(filepath: str, description: str = "文件") -> bool:
    if not Path(filepath).is_file():
        logger.error(f"{description}不存在：{filepath}")
        return False
    return True


def rms(samples_float32: np.ndarray) -> float:
    if samples_float32.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples_float32), dtype=np.float64)))
