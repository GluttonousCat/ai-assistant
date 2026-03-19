# -*- encoding: utf-8 -*-
from __future__ import annotations

import os

import requests
from .utils import get_logger

TOP_DIR = os.path.dirname(os.path.abspath(__file__))
SERVER_BASE_URL = "http://localhost:9897"
logger = get_logger(__name__)

class ServerNotifier:
    def __init__(self, SERVER_BASE_URL):
        self._url = SERVER_BASE_URL
        pass

    def notify_server_interrupt(self):
        try:
            requests.get(f"{self._url}/interrupt_play", timeout=0.5)
        except Exception as e:
            logger.error(f"Failed to notify server to interrupt: {e}")
            pass

    def notify_server_to_upload(self, filename: str):
        try:
            abs_path = os.path.abspath(filename)
            logger.info(f"\U0001f4e4 通知服务器上传文件: {abs_path}")
            requests.get(
                f"{self._url}/do_send",
                params={"fname": abs_path},
                timeout=10,
            )
        except Exception as e:
            logger.error(f"Failed to notify server to upload: {e}")

    def notify_server_play_preset(self) -> float:
        try:
            resp = requests.get(f"{self._url}/play_preset", timeout=5)
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            duration = float(data.get("duration") or 0.0)
            return max(0.0, duration)
        except Exception as e:
            logger.error(f"Failed to notify server to upload: {e}")
            return 0.0

    def notify_server_kws_ready(self):
        try:
            params = {}
            sound_file = os.environ.get("KWS_READY_SOUND_FILE", "").strip()
            if sound_file:
                params["file"] = sound_file
            style = os.environ.get("KWS_READY_SOUND_STYLE", "").strip()
            if style:
                params["style"] = style
            vol = os.environ.get("KWS_READY_SOUND_VOL", "").strip()
            if vol:
                params["vol"] = vol
            requests.get(f"{self._url}/beep_ready", params=params, timeout=0.5)
        except Exception as e:
            logger.error(f"Failed to notify server to upload: {e}")
            pass
