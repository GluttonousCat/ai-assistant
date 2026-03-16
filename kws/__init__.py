#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KWS - 关键词检测包
Keyword Spotting package based on Sherpa-ONNX
"""

from .detector import KeywordDetector, DetectionResult
from .audio import AudioRecorder, AudioPlayer
from .server_notify import ServerNotifier
from .utils import check_file_exists, rms

__all__ = [
    "KeywordDetector",
    "DetectionResult",
    "AudioRecorder",
    "AudioPlayer",
    "ServerNotifier",
    "check_file_exists",
    "rms",
]

__version__ = "1.0.0"
