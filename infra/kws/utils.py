# -*- coding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
import numpy as np


def get_logger(name: str) -> logging.Logger:
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    return logging.getLogger(name)


def check_file_exists(filepath: str, description: str = "File") -> bool:
    if not Path(filepath).is_file():
        logging.error(f"{description} not found: {filepath}")
        return False
    return True


def rms(samples_float32: np.ndarray) -> float:
    if samples_float32.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(samples_float32), dtype=np.float64)))


class AudioMonitor:
    def __init__(self, interval_seconds: int = 5):
        self.interval = interval_seconds
        self.last_summary_time = time.time()
        self.logger = get_logger("VoiceMonitor")

        self.passive_rms = []
        self.active_rms = []
        self.min_rms = float('inf')
        self.max_rms = 0.0

    def add_sample(self, rms_val: float, state: str):
        if rms_val < self.min_rms: self.min_rms = rms_val
        if rms_val > self.max_rms: self.max_rms = rms_val

        if state == "PASSIVE":
            self.passive_rms.append(rms_val)
        else:
            self.active_rms.append(rms_val)

        now = time.time()
        if now - self.last_summary_time >= self.interval:
            self._print_summary()
            self.last_summary_time = now

    def _print_summary(self):
        def calc_stats(vals):
            if not vals: return "No Data"
            return (f"min={min(vals):.6f} max={max(vals):.6f} "
                    f"avg={(sum(vals) / len(vals)):.6f}")

        p_stat = calc_stats(self.passive_rms)
        a_stat = calc_stats(self.active_rms)

        # Translated summary log
        self.logger.info(
            f"🔊[{self.interval}s Audio Monitor] Global Min={self.min_rms:.6f} Max={self.max_rms:.6f} | "
            f"Passive State: {p_stat} | Active State: {a_stat}")

        self.passive_rms.clear()
        self.active_rms.clear()
        self.min_rms = float('inf')
        self.max_rms = 0.0