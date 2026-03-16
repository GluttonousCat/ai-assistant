#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
工具函数模块
"""

import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


def check_file_exists(filepath: str, description: str = "文件") -> bool:
    """检查文件是否存在"""
    if not Path(filepath).is_file():
        logger.error(f"{description}不存在：{filepath}")
        return False
    return True


def rms(samples_float32: np.ndarray) -> float:
    """计算音频 RMS 音量"""
    if samples_float32.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples_float32), dtype=np.float64)))
