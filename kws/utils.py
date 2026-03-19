# -*- coding: utf-8 -*-
from __future__ import annotations

import logging
from pathlib import Path
import numpy as np


def get_logger(name: str) -> logging.Logger:
    """获取标准化配置的 Logger"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger(name)


def check_file_exists(filepath: str, description: str = "文件") -> bool:
    """检查文件是否存在"""
    if not Path(filepath).is_file():
        logging.error(f"{description}不存在：{filepath}")
        return False
    return True


def rms(samples_float32: np.ndarray) -> float:
    """计算音频块的 RMS (均方根) 能量值"""
    if samples_float32.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples_float32), dtype=np.float64)))