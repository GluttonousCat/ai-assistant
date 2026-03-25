# -*- encoding: utf-8 -*-
"""
@date: 2026/03/23
@author: GluttonousCat
"""
from __future__ import annotations

import os
import requests
from .utils import get_logger

SERVER_BASE_URL = "http://localhost:9897"
logger = get_logger(__name__)


class ServerNotifier:
    def __init__(self, base_url: str = SERVER_BASE_URL):
        self._url = base_url

    def interrupt_play(self) -> None:
        # Modified: Sending TTS interrupt request
        logger.info("🛑 Sending TTS interrupt request...")
        try:
            resp = requests.get(f"{self._url}/interrupt_play", timeout=0.5)
            if resp.status_code == 200:
                # Modified: Interruption successful
                logger.debug("✅ Interruption successful")
        except Exception as e:
            # Modified: Interruption failed
            logger.error(f"❌ Interruption failed: {e}")

    def to_upload(self, filename: str) -> None:
        try:
            abs_path = os.path.abspath(filename)
            # Modified: Sending audio upload request
            logger.info(f"📤 Sending audio upload request: {abs_path}")
            requests.get(
                f"{self._url}/do_send",
                params={"fname": abs_path},
                timeout=10,
            )
        except Exception as e:
            # Modified: Upload request failed
            logger.error(f"❌ Upload request failed: {e}")

    def play_preset(self) -> float:
        try:
            resp = requests.get(f"{self._url}/play_preset", timeout=5)
            if resp.status_code != 200:
                return 0.0
            data = resp.json()
            duration = float(data.get("duration") or 0.0)
            return max(0.0, duration)
        except Exception as e:
            # Modified: Failed to play preset
            logger.error(f"❌ Failed to play preset: {e}")
            return 0.0

    def kws_ready(self) -> None:
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
            # Modified: Initialization notification failed
            logger.error(f"❌ Initialization notification failed: {e}")