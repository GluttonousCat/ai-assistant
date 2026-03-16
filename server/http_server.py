#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HTTP Server 模块 - 本地 HTTP API 服务器
提供录音控制、播放控制等接口
"""

import asyncio
import json
import os
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from .config import config


class MyHttpHandler(BaseHTTPRequestHandler):
    """HTTP 请求处理器"""
    client = None  # AudioClient 实例，由外部注入

    def do_GET(self):
        """处理 GET 请求"""
        parsed_path = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_path.query)

        if parsed_path.path == '/do_send':
            self._handle_do_send(params)

        elif parsed_path.path == '/record_start':
            self._handle_record_start()

        elif parsed_path.path == '/record_stop':
            self._handle_record_stop()

        elif parsed_path.path == '/interrupt_play':
            self._handle_interrupt_play()

        elif parsed_path.path == '/play_preset':
            self._handle_play_preset(params)

        elif parsed_path.path == '/beep_ready':
            self._handle_beep_ready(params)

        elif parsed_path.path == '/set_volume':
            self._handle_set_volume(params)

        else:
            self.send_error(404)

    def _handle_do_send(self, params):
        """处理录音上传请求"""
        if 'fname' not in params:
            self.send_error(400, "Missing fname")
            return
        fname = params['fname'][0]
        asyncio.run_coroutine_threadsafe(
            self.client.send_recording_to_api(fname),
            self.client.loop
        )
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Upload task scheduled")

    def _handle_record_start(self):
        """处理录音开始请求（兼容保留）"""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Recording started (dummy)")

    def _handle_record_stop(self):
        """处理录音停止请求（兼容保留）"""
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Recording stopped (dummy)")

    def _handle_interrupt_play(self):
        """处理中断播放请求"""
        if self.client:
            self.client.clear_audio_queue()
            try:
                self.client.notify_server_interrupt()
            except Exception:
                pass
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Playback interrupted")

    def _handle_play_preset(self, params):
        """处理播放预设音频请求"""
        if not self.client:
            self.send_error(500, "No client")
            return
        try:
            self.client.clear_audio_queue()
        except Exception:
            pass
        if 'file' in params and params['file']:
            f = params['file'][0]
            self.client._preset_items = [f]

        future = asyncio.run_coroutine_threadsafe(self.client.play_random_preset(), self.client.loop)
        try:
            duration = future.result(timeout=10)
        except Exception:
            duration = 0.0
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        payload = {'duration': duration}
        try:
            payload.update(self.client.get_preset_status())
        except Exception:
            pass
        self.wfile.write(json.dumps(payload).encode('utf-8'))

    def _handle_beep_ready(self, params):
        """处理准备提示音请求"""
        if not self.client:
            self.send_error(500, "No client")
            return
        try:
            file_param = params.get('file', [""])[0]
            env_file = os.environ.get('KWS_READY_SOUND_FILE', '')
            file_path = (file_param or env_file or '').strip()
            if not file_path:
                try:
                    default_bootup = Path(__file__).resolve().parent.parent / 'bootup.wav'
                    if default_bootup.exists() and default_bootup.is_file():
                        file_path = str(default_bootup)
                except Exception:
                    pass
            style = (params.get('style', [""])[0] or os.environ.get('KWS_READY_SOUND_STYLE', 'chime')).strip().lower()
            vol = float(params.get('vol', [os.environ.get('KWS_READY_SOUND_VOL', '0.14')])[0] or '0.14')
            freq = float(params.get('freq', ["660"])[0] or "660")
            ms = int(params.get('ms', ["140"])[0] or "140")
        except Exception:
            file_path = ''
            style = 'chime'
            freq, ms, vol = 660.0, 140, 0.14

        try:
            duration = 0.0
            if file_path:
                duration = self.client.enqueue_wav_file(file_path, volume=1.0)
            else:
                if style == 'beep':
                    duration = self.client.enqueue_beep(freq_hz=freq, duration_ms=ms, volume=vol)
                else:
                    d1 = self.client.enqueue_beep(freq_hz=523.25, duration_ms=120, volume=vol)
                    d2 = self.client.enqueue_beep(freq_hz=659.25, duration_ms=140, volume=vol)
                    duration = float(d1 + d2)
        except Exception:
            duration = 0.0

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'duration': duration, 'style': style, 'file': file_path}).encode('utf-8'))

    def _handle_set_volume(self, params):
        """处理音量设置请求"""
        if 'v' in params:
            try:
                vol = float(params['v'][0])
                self.client.output_volume = vol
                print(f"🔊 Volume set to {vol}x")
                msg = f"Volume set to {vol}"
            except ValueError:
                msg = "Invalid volume value"
        else:
            msg = "Missing 'v' param (usage: /set_volume?v=3.0)"

        self.send_response(200)
        self.end_headers()
        self.wfile.write(msg.encode())


def create_http_server(client, port: int = 9897) -> HTTPServer:
    """创建 HTTP 服务器"""
    MyHttpHandler.client = client
    http_server = HTTPServer(('0.0.0.0', port), MyHttpHandler)
    return http_server
