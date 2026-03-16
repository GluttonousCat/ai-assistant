#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
配置模块 - 全局配置与工具函数
"""

import os
from pathlib import Path


class Config:
    """全局配置类"""

    def __init__(self):
        # WebSocket / HTTP 配置
        self.ws_url = os.getenv('WS_URL', 'wss://gspast.inspures.com/ws/xiaozhi/v1/')
        self.api_url = os.getenv('API_URL', 'https://gspast.inspures.com/api/chat/upload-audio')

        self.preset_role_id = os.getenv('PRESET_ROLE_ID', os.getenv('ROLE_ID', ''))
        self.preset_cache_dir = os.getenv(
            'PRESET_CACHE_DIR',
            os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'preset_cache')
        )
        self.preset_warmup_sync_count = int(os.getenv('PRESET_WARMUP_SYNC_COUNT', '1') or '1')

        # 音频参数
        self.input_sample_rate = 16000  # 录音采样率
        self.output_sample_rate = 24000  # 播放采样率
        self.channels = 1
        self.frame_duration = 60  # ms

        # 录音完成提示音
        self.dong_sound_file = os.getenv(
            'DONG_SOUND_FILE',
            str(Path(__file__).resolve().parent.parent / 'dong.wav')
        )

        # 计算帧大小
        self.input_frame_size = int(self.input_sample_rate * (self.frame_duration / 1000))
        self.output_frame_size = int(self.output_sample_rate * (self.frame_duration / 1000))


# 全局配置实例
config = Config()

# 全局端口
HTTP_PORT = 9897
WS_PORT = 9898


def downmix_to_mono(data, keepdims=False):
    """将多声道混音为单声道"""
    if data.ndim > 1 and data.shape[1] > 1:
        mono = np.mean(data, axis=1)
        if keepdims:
            return mono[:, np.newaxis]
        return mono
    return data


def upmix_mono_to_channels(data, channels):
    """单声道转多声道"""
    if channels == 1:
        return data[:, np.newaxis] if data.ndim == 1 else data
    return np.tile(data[:, np.newaxis], (1, channels))


# 导入 numpy（放在最后避免循环依赖）
import numpy as np
