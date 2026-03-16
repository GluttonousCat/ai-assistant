#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AudioClient 模块 - 音频客户端核心类
负责 WebSocket 通信、音频编解码、重采样和播放
"""

import asyncio
import json
import os
import queue
import random
import subprocess
import threading
import time
import traceback
import wave
from collections import deque
from pathlib import Path
from typing import Optional

import numpy as np
import opuslib
import requests
import soxr
import sounddevice as sd
import websockets

from .config import config, downmix_to_mono, upmix_mono_to_channels


# 【修复】尝试导入 State 枚举，如果失败则手动定义
try:
    from websockets.protocol import State
except ImportError:
    class State:
        OPEN = 1
        CLOSED = 3


class AudioClient:
    """
    整合了网络通信与高质量音频处理的客户端
    采用 soxr 重采样 (24k->48k) + deque 缓冲池，彻底解决音质问题
    """

    def __init__(self):
        # --- 网络相关 ---
        self.device_id = os.getenv('DEVICE_ID', 'dev-device')
        self.access_token = os.getenv('ACCESS_TOKEN', 'test-token')
        self.HEADERS = {
            "Authorization": f"Bearer {self.access_token}",
            "Protocol-Version": "1",
            "Device-Id": self.device_id,
            "Client-Id": self.device_id,
        }
        self.session_id = None
        self.websocket = None
        self.handing_message_task = None
        self.loop = None  # asyncio loop 引用
        self._reconnecting_lock = asyncio.Lock()  # 防止并发重连

        # --- 音频设备参数 ---
        self.input_device_id = None
        self.output_device_id = None

        # 【音量增强】默认 1.5 倍
        self.output_volume = 1.5

        # 实际硬件采样率 (初始化时检测)
        self.device_input_sample_rate = config.input_sample_rate
        self.device_output_sample_rate = 44100  # 默认值，会被实际硬件覆盖
        self.input_channels = 1
        self.output_channels = 1

        # Opus 编解码器
        self.opus_encoder = None
        self.opus_decoder = None

        # 流
        self.input_stream = None
        self.output_stream = None

        # --- 关键：重采样与缓冲 ---
        # 1. 播放缓冲：两级结构
        self._output_buffer = queue.Queue(maxsize=500)
        self._resample_output_buffer = deque()

        # 2. 重采样器
        self.output_resampler = None  # soxr instance

        # 状态
        self._is_recording = False
        self._recorded_frames = []

        self._preset_items = []
        self._last_preset_error = None
        self._last_preset_error_detail = None
        self._last_preset_chosen = None
        self._preset_pcm_cache = {}

        # 录音完成提示音缓存
        self._dong_pcm = None
        self._dong_duration = 0.0

    # ==================== 连接状态检查 ====================

    @property
    def is_ws_connected(self) -> bool:
        """兼容所有 websockets 版本的连接状态检查"""
        if self.websocket is None:
            return False

        # 1. 优先尝试 state 属性 (websockets 10.0+ / 14.0+)
        state = getattr(self.websocket, 'state', None)
        if state is not None:
            return state == State.OPEN or state == 1

        # 2. 回退尝试 closed 属性 (websockets < 10.0)
        is_closed = getattr(self.websocket, 'closed', True)
        return not is_closed

    # ==================== 预设音频管理 ====================

    def load_local_preset_cache(self):
        """加载本地预设缓存"""
        try:
            base = Path(config.preset_cache_dir)
            if not base.exists() or not base.is_dir():
                return
            files = [str(p) for p in base.rglob('*.wav') if p.is_file()]
            if files:
                self._preset_items = files
                self._last_preset_error = None
                self._warmup_preset_pcm_cache_async()
        except Exception:
            return

    def _warmup_preset_pcm_cache_async(self):
        """异步预热预设 PCM 缓存"""
        try:
            threading.Thread(target=self._warmup_preset_pcm_cache, daemon=True).start()
        except Exception:
            return

    def _warmup_preset_pcm_cache(self):
        """预热预设 PCM 缓存"""
        try:
            items = list(self._preset_items) if self._preset_items else []
            for fp in items:
                try:
                    self._get_or_build_preset_pcm(fp)
                except Exception:
                    continue
        except Exception:
            return

    def _get_or_build_preset_pcm(self, file_path: str):
        """获取或构建预设 PCM 数据"""
        cached = self._preset_pcm_cache.get(file_path)
        if cached is not None:
            return cached

        wav_sr, wav_audio = self._load_wav_float32(file_path)
        if wav_audio is None or len(wav_audio) == 0:
            raise RuntimeError('empty_audio')

        if wav_sr != config.output_sample_rate:
            rs = soxr.ResampleStream(wav_sr, config.output_sample_rate, num_channels=1, dtype="float32", quality="QQ")
            wav_audio = rs.resample_chunk(wav_audio, last=True)

        wav_audio = wav_audio.astype(np.float32, copy=False)
        self._preset_pcm_cache[file_path] = wav_audio
        return wav_audio

    def warmup_preset_pcm_cache_sync(self, count: int = 1):
        """同步预热预设 PCM 缓存"""
        try:
            if not self._preset_items:
                self.load_local_preset_cache()
            if not self._preset_items:
                self.prefetch_preset_audios()
            if not self._preset_items:
                return

            n = int(count) if count is not None else 0
            if n <= 0:
                return

            candidates = sorted([p for p in self._preset_items if str(p).lower().endswith('.wav')])
            for fp in candidates[:n]:
                try:
                    self._get_or_build_preset_pcm(fp)
                except Exception:
                    continue
        except Exception:
            return

    def get_preset_status(self):
        """获取预设状态"""
        return {
            'cached_count': len(self._preset_items) if self._preset_items else 0,
            'last_error': self._last_preset_error,
            'last_error_detail': self._last_preset_error_detail,
            'chosen': self._last_preset_chosen,
        }

    # ==================== 提示音管理 ====================

    def _load_dong_sound(self):
        """预加载录音完成提示音到内存"""
        try:
            fp = str(config.dong_sound_file or '').strip()
            if not fp:
                return
            if not os.path.exists(fp):
                print(f"ℹ️ Dong sound file not found: {fp}")
                return

            wav_sr, wav_audio = self._load_wav_float32(fp)
            if wav_audio is None or len(wav_audio) == 0:
                print(f"⚠️ Dong sound empty: {fp}")
                return

            if wav_sr != config.output_sample_rate:
                rs = soxr.ResampleStream(
                    wav_sr,
                    config.output_sample_rate,
                    num_channels=1,
                    dtype="float32",
                    quality="QQ"
                )
                wav_audio = rs.resample_chunk(wav_audio.astype(np.float32, copy=False), last=True)

            wav_audio = np.clip(wav_audio.astype(np.float32, copy=False), -1.0, 1.0)
            self._dong_pcm = wav_audio
            self._dong_duration = float(len(wav_audio) / float(config.output_sample_rate)) if config.output_sample_rate else 0.0
            print(f"🔔 Dong sound loaded: {fp}, duration={self._dong_duration:.2f}s")
        except Exception as e:
            print(f"⚠️ Failed to load dong sound: {e}")

    def play_dong_sound(self) -> float:
        """将预加载的提示音压入播放队列"""
        try:
            if self._dong_pcm is None or len(self._dong_pcm) == 0:
                return 0.0
            step = max(1, int(config.output_sample_rate * 0.05))
            for i in range(0, len(self._dong_pcm), step):
                chunk = self._dong_pcm[i:i + step]
                try:
                    self._output_buffer.put_nowait(chunk)
                except queue.Full:
                    break
            return self._dong_duration
        except Exception:
            return 0.0

    def enqueue_beep(self, freq_hz: float = 880.0, duration_ms: int = 180, volume: float = 0.18) -> float:
        """生成并播放提示音"""
        try:
            sr = int(config.output_sample_rate)
            if sr <= 0:
                return 0.0
            ms = int(duration_ms) if duration_ms is not None else 0
            if ms <= 0:
                return 0.0
            n = int(sr * (ms / 1000.0))
            if n <= 0:
                return 0.0

            t = (np.arange(n, dtype=np.float32) / float(sr))
            tone = (np.sin(2.0 * np.pi * float(freq_hz) * t) * float(volume)).astype(np.float32, copy=False)

            ramp = min(int(sr * 0.01), max(1, n // 10))
            if ramp > 1:
                w = np.linspace(0.0, 1.0, ramp, dtype=np.float32)
                tone[:ramp] *= w
                tone[-ramp:] *= w[::-1]

            step = max(1, int(sr * 0.05))
            for i in range(0, len(tone), step):
                chunk = tone[i:i + step]
                try:
                    self._output_buffer.put_nowait(chunk)
                except queue.Full:
                    break
            return float(ms / 1000.0)
        except Exception:
            return 0.0

    def enqueue_wav_file(self, file_path: str, volume: float = 1.0) -> float:
        """播放 WAV 文件"""
        try:
            fp = str(file_path or '').strip()
            if not fp:
                return 0.0
            if not os.path.exists(fp):
                return 0.0

            wav_sr, wav_audio = self._load_wav_float32(fp)
            if wav_audio is None or len(wav_audio) == 0:
                return 0.0

            if wav_sr != config.output_sample_rate:
                rs = soxr.ResampleStream(wav_sr, config.output_sample_rate, num_channels=1, dtype="float32", quality="QQ")
                wav_audio = rs.resample_chunk(wav_audio.astype(np.float32, copy=False), last=True)

            wav_audio = wav_audio.astype(np.float32, copy=False) * float(volume)
            wav_audio = np.clip(wav_audio, -1.0, 1.0)

            duration = float(len(wav_audio) / float(config.output_sample_rate)) if config.output_sample_rate else 0.0

            step = max(1, int(config.output_sample_rate * 0.05))
            for i in range(0, len(wav_audio), step):
                chunk = wav_audio[i:i + step]
                try:
                    self._output_buffer.put_nowait(chunk)
                except queue.Full:
                    break

            return duration
        except Exception:
            return 0.0

    # ==================== 音频文件加载 ====================

    def _load_wav_float32(self, file_path: str):
        """加载 WAV 文件为 float32 格式"""
        with open(file_path, 'rb') as f:
            head = f.read(12)

        is_wav = len(head) >= 12 and head[0:4] == b'RIFF' and head[8:12] == b'WAVE'
        wave_error = None
        if is_wav:
            try:
                with wave.open(file_path, 'rb') as wf:
                    channels = wf.getnchannels()
                    sampwidth = wf.getsampwidth()
                    fr = wf.getframerate()
                    frames = wf.readframes(wf.getnframes())

                if sampwidth == 2:
                    audio_int16 = np.frombuffer(frames, dtype=np.int16)
                    audio_float = audio_int16.astype(np.float32) / 32768.0
                elif sampwidth == 4:
                    audio_int32 = np.frombuffer(frames, dtype=np.int32)
                    audio_float = audio_int32.astype(np.float32) / 2147483648.0
                else:
                    audio_int16 = np.frombuffer(frames, dtype=np.int16)
                    audio_float = audio_int16.astype(np.float32) / 32768.0

                if channels > 1:
                    audio_float = audio_float.reshape(-1, channels)
                    audio_float = np.mean(audio_float, axis=1)

                return fr, audio_float
            except Exception as e:
                wave_error = e

        try:
            cmd = [
                "ffmpeg",
                "-v", "error",
                "-i", file_path,
                "-f", "s16le",
                "-ac", "1",
                "-ar", str(config.output_sample_rate),
                "pipe:1",
            ]
            proc = subprocess.run(cmd, capture_output=True)
            if proc.returncode != 0 or not proc.stdout:
                raise RuntimeError(proc.stderr.decode('utf-8', errors='ignore')[:200])
            audio_int16 = np.frombuffer(proc.stdout, dtype=np.int16)
            audio_float = audio_int16.astype(np.float32) / 32768.0
            return config.output_sample_rate, audio_float
        except FileNotFoundError as e:
            raise RuntimeError(f"ffmpeg_not_found head={head.hex()}") from e
        except Exception as e:
            wave_info = ''
            if wave_error is not None:
                wave_info = f" wave={type(wave_error).__name__}:{wave_error!r}"
            raise RuntimeError(f"decode_failed head={head.hex()}{wave_info} ffmpeg={type(e).__name__}:{e!r}") from e

    # ==================== 预设音频获取 ====================

    def _http_get_json(self, url, params=None, timeout=10):
        """HTTP GET 请求"""
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def _download_file(self, url, target_path: Path, timeout=20):
        """下载文件"""
        try:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with requests.get(url, stream=True, timeout=timeout) as r:
                if r.status_code != 200:
                    return False
                with open(target_path, 'wb') as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            return True
        except Exception:
            return False

    def _server_http_base(self):
        """获取服务器 HTTP 基础 URL"""
        try:
            parsed = urllib.parse.urlparse(config.api_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return "http://127.0.0.1:8091"

    def prefetch_preset_audios(self):
        """预取预设音频"""
        import urllib.parse
        base = self._server_http_base()
        manifest_url = f"{base}/api/preset/manifest"

        role_id = str(config.preset_role_id).strip()
        params = {'roleId': role_id} if role_id else None
        payload = self._http_get_json(manifest_url, params=params, timeout=10)
        if not payload or 'data' not in payload:
            self._last_preset_error = 'manifest_request_failed'
            self.load_local_preset_cache()
            return

        data = payload.get('data') or {}
        manifest = data.get('manifest')
        if not manifest:
            self._last_preset_error = 'manifest_empty'
            self.load_local_preset_cache()
            return

        if isinstance(manifest, list):
            manifest = manifest[0] if manifest else None
        if not isinstance(manifest, dict):
            self._last_preset_error = 'manifest_invalid'
            self.load_local_preset_cache()
            return

        voice_dir = (manifest.get('voiceDir') or 'default').strip()
        items = manifest.get('items') or []
        cache_base = Path(config.preset_cache_dir) / voice_dir
        cache_base.mkdir(parents=True, exist_ok=True)

        cached = []
        for item in items:
            if not isinstance(item, dict):
                continue
            url_path = item.get('url')
            if not url_path:
                continue
            file_name = os.path.basename(urllib.parse.urlparse(url_path).path)
            if not file_name:
                continue
            if not file_name.lower().endswith('.wav'):
                continue
            local_path = cache_base / file_name
            if not local_path.exists():
                ok = self._download_file(f"{base}{url_path}", local_path)
                if not ok:
                    continue
            cached.append(str(local_path))

        self._preset_items = cached
        self._last_preset_error = None
        print(f"📦 Preset cache ready. roleId={role_id if role_id else 'AUTO'} voiceDir={voice_dir} files={len(cached)}")
        self._warmup_preset_pcm_cache_async()

    async def play_random_preset(self) -> float:
        """播放随机预设音频"""
        self._last_preset_error = None
        self._last_preset_error_detail = None
        self._last_preset_chosen = None
        if not self._preset_items:
            try:
                self.load_local_preset_cache()
                if not self._preset_items:
                    self.prefetch_preset_audios()
            except Exception:
                pass
        if not self._preset_items:
            self._last_preset_error = 'no_cached_files'
            return 0.0

        warmed = [p for p in self._preset_items if p in self._preset_pcm_cache]
        if warmed:
            choice = random.choice(warmed)
        else:
            choice = random.choice(self._preset_items)
        self._last_preset_chosen = choice
        if not str(choice).lower().endswith('.wav'):
            self._last_preset_error = 'unsupported_audio_format'
            return 0.0

        try:
            pcm = self._get_or_build_preset_pcm(choice)
        except Exception as e:
            self._last_preset_error = 'wav_decode_failed'
            self._last_preset_error_detail = f"{type(e).__name__}:{e!r}"
            return 0.0

        duration = float(len(pcm) / float(config.output_sample_rate)) if config.output_sample_rate else 0.0

        try:
            step = max(1, int(config.output_sample_rate * 0.2))
            for i in range(0, len(pcm), step):
                chunk = pcm[i:i + step]
                try:
                    self._output_buffer.put_nowait(chunk)
                except queue.Full:
                    break
        except Exception:
            return 0.0

        return duration

    # ==================== 音频初始化 ====================

    async def initialize_audio(self):
        """初始化音频设备、编解码器及重采样器"""
        try:
            # 1. 自动检测设备能力
            self._detect_device_capabilities()

            # 2. 创建 Opus 编解码器
            self.opus_encoder = opuslib.Encoder(
                config.input_sample_rate,
                config.channels,
                opuslib.APPLICATION_VOIP,
            )
            self.opus_decoder = opuslib.Decoder(
                config.output_sample_rate,
                config.channels
            )

            # 3. 创建输出重采样器
            if self.device_output_sample_rate != config.output_sample_rate:
                print(f"🔧 初始化高质量重采样：{config.output_sample_rate}Hz -> {self.device_output_sample_rate}Hz")
                self.output_resampler = soxr.ResampleStream(
                    config.output_sample_rate,
                    self.device_output_sample_rate,
                    num_channels=1,
                    dtype="float32",
                    quality="QQ"
                )
            else:
                self.output_resampler = None

            # 4. 创建音频流
            output_block_size = int(self.device_output_sample_rate * (config.frame_duration / 1000))

            self.input_stream = None

            print(f"🔊 输出流配置：Rate={self.device_output_sample_rate}Hz, Block={output_block_size}")
            self.output_stream = sd.OutputStream(
                device=self.output_device_id,
                samplerate=self.device_output_sample_rate,
                channels=self.output_channels,
                dtype=np.float32,
                blocksize=output_block_size,
                callback=self._output_callback,
                latency="low",
            )

            self.output_stream.start()
            print("✅ 音频系统初始化完成")

        except Exception as e:
            print(f"❌ 初始化音频设备失败：{e}")
            raise

    def _detect_device_capabilities(self):
        """检测输入输出设备的实际能力"""
        try:
            in_dev = sd.query_devices(kind='input')
            out_dev = sd.query_devices(kind='output')

            self.input_channels = min(in_dev['max_input_channels'], 2)
            self.output_channels = min(out_dev['max_output_channels'], 2)

            self.device_input_sample_rate = int(in_dev['default_samplerate'])
            self.device_output_sample_rate = int(out_dev['default_samplerate'])

            print(f"Hardware: Input({self.device_input_sample_rate}Hz), Output({self.device_output_sample_rate}Hz)")
        except Exception as e:
            print(f"Device detection failed, using defaults: {e}")
            self.device_input_sample_rate = 16000
            self.device_output_sample_rate = 48000

    def clear_audio_queue(self):
        """强制清空播放队列，实现立即静音/打断"""
        try:
            while not self._output_buffer.empty():
                self._output_buffer.get_nowait()
        except queue.Empty:
            pass

        self._resample_output_buffer.clear()
        print("🛑 收到唤醒指令，播放流已强制中断 (Buffer Cleared)")

    def notify_server_interrupt(self, reason: str = "wake_word"):
        """通知服务器中断播放"""
        try:
            if not self.session_id:
                return
            parsed = urllib.parse.urlparse(config.api_url)
            base = f"{parsed.scheme}://{parsed.netloc}"
            url = f"{base}/api/chat/interrupt"
            requests.post(url, params={'sessionId': self.session_id, 'reason': reason}, timeout=1)
        except Exception:
            return

    # ==================== 音频回调 ====================

    def _output_callback(self, outdata, frames, time_info, status):
        """输出回调 - 负责重采样、音量调节与混音"""
        if status and "underflow" not in str(status).lower():
            pass

        try:
            # 阶段 1: 填充缓冲区
            while len(self._resample_output_buffer) < frames:
                try:
                    audio_packet = self._output_buffer.get_nowait()
                    if self.output_resampler:
                        resampled_data = self.output_resampler.resample_chunk(audio_packet, last=False)
                        if len(resampled_data) > 0:
                            self._resample_output_buffer.extend(resampled_data)
                    else:
                        self._resample_output_buffer.extend(audio_packet)
                except queue.Empty:
                    break

            # 阶段 2: 消费缓冲区
            if len(self._resample_output_buffer) >= frames:
                frame_data = [self._resample_output_buffer.popleft() for _ in range(frames)]
                mono_samples = np.array(frame_data, dtype=np.float32)

                # 【音量增强】
                mono_samples = mono_samples * self.output_volume
                # 【防爆音保护】
                mono_samples = np.clip(mono_samples, -1.0, 1.0)

                if self.output_channels > 1:
                    outdata[:] = upmix_mono_to_channels(mono_samples, self.output_channels)
                else:
                    outdata[:, 0] = mono_samples
            else:
                outdata.fill(0)

        except Exception as e:
            print(f"Output callback error: {e}")
            outdata.fill(0)

    # ==================== 音频操作 ====================

    async def write_audio(self, opus_data: bytes):
        """解码 (24k) -> 放入播放队列"""
        try:
            # 1. 解码：得到 24000Hz PCM
            pcm_data = self.opus_decoder.decode(opus_data, config.output_frame_size)
            audio_int16 = np.frombuffer(pcm_data, dtype=np.int16)
            audio_float = audio_int16.astype(np.float32) / 32768.0

            # 2. 入队
            try:
                self._output_buffer.put_nowait(audio_float)
            except queue.Full:
                print("⚠️ 播放队列已满，丢弃音频帧")

        except opuslib.OpusError as e:
            print(f"Opus decode error: {e}")
        except Exception as e:
            print(f"Write audio error: {e}")

    # 服务端不处理输入逻辑
    async def start_recording(self):
        pass

    async def stop_recording(self) -> Optional[str]:
        pass

    # ==================== WebSocket 通信 ====================

    async def connect(self):
        """建立 WebSocket 连接"""
        if self.websocket:
            try:
                await self.websocket.close()
            except:
                pass

        print(f"Connecting to WS: {config.ws_url}")
        self.websocket = await websockets.connect(
            uri=config.ws_url,
            additional_headers=self.HEADERS,
            ping_interval=20,
            ping_timeout=20,
            close_timeout=10,
            max_size=10 * 1024 * 1024
        )
        print("Websocket connected!")

    async def sayhello(self):
        """发送 Hello 包并请求 24k Audio 特性"""
        print(f"Sending hello with OutputSR={config.output_sample_rate}")
        hello_message = {
            "type": "hello",
            "version": 1,
            "features": {"audio": True},
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": config.output_sample_rate,
                "channels": config.channels,
                "frame_duration": config.frame_duration,
            },
        }
        if self.is_ws_connected:
            await self.websocket.send(json.dumps(hello_message))
        else:
            raise ConnectionError("Cannot say hello: Websocket not open")

    async def handle_server_hello(self, data: dict):
        """处理服务器 Hello 响应"""
        self.session_id = data.get("session_id")
        if self.session_id:
            print(f"✅ Session ID acquired: {self.session_id}")
        else:
            print("⚠️ Server Hello received but no Session ID")

    async def message_handler(self):
        """持续处理消息"""
        try:
            async for message in self.websocket:
                try:
                    if isinstance(message, str):
                        data = json.loads(message)
                        if data.get("type") == "hello":
                            await self.handle_server_hello(data)
                        elif data.get("type") == "tts" and data.get("state") == "stop":
                            pass
                        else:
                            pass
                    elif isinstance(message, bytes):
                        await self.write_audio(message)
                except Exception as e:
                    print(f"Msg processing error: {e}")
                    continue
        except Exception as e:
            print(f"⚠️ WebSocket loop disconnected: {e}")
        finally:
            self.session_id = None
            if self.is_ws_connected:
                await self.websocket.close()

    async def reconnect(self):
        """带锁的重连逻辑"""
        if self._reconnecting_lock.locked():
            return

        async with self._reconnecting_lock:
            print("🔄 Reconnecting logic started...")

            if self.handing_message_task and not self.handing_message_task.done():
                self.handing_message_task.cancel()
                try:
                    await self.handing_message_task
                except asyncio.CancelledError:
                    pass

            self.session_id = None
            backoff = 1
            while True:
                try:
                    await self.connect()
                    await self.sayhello()
                    self.handing_message_task = asyncio.create_task(self.message_handler())
                    for _ in range(20):
                        if self.session_id:
                            print("✅ Reconnected and Session ID ready.")
                            return
                        await asyncio.sleep(0.1)
                    print("Connected but no Session ID, retrying...")
                except Exception as e:
                    print(f"Reconnect failed: {e}. Retrying in {backoff}s...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 10)

    async def check_and_restore_connection(self) -> bool:
        """主动检查连接状态"""
        is_socket_alive = self.is_ws_connected
        is_session_valid = self.session_id is not None

        if is_socket_alive and is_session_valid:
            return True

        print(f"⚠️ Connection check failed (Socket={is_socket_alive}, Session={is_session_valid}). Restoring...")
        await self.reconnect()

        if self.is_ws_connected and self.session_id:
            return True
        return False

    async def send_recording_to_api(self, filename):
        """上传录音文件，上传前强制检查连接"""
        import urllib.parse
        if not os.path.exists(filename):
            print(f"File not found: {filename}")
            return False

        try:
            connection_ok = await self.check_and_restore_connection()
            if not connection_ok:
                print("❌ Failed to restore connection. Cannot upload.")
                return False

            print(f"📤 Uploading {filename} with Session ID: {self.session_id}")
            data = {'sessionId': self.session_id}

            with open(filename, 'rb') as wav_file:
                files = {'file': (filename, wav_file, 'audio/wav')}
                response = requests.post(config.api_url, files=files, data=data, timeout=10)

            if response.status_code == 200:
                print(f"✅ Upload success: {response.text}")
                try:
                    self.play_dong_sound()
                except Exception:
                    pass
                return True
            else:
                print(f"❌ Upload failed: {response.status_code} - {response.text}")
                if response.status_code in [400, 401, 403, 500]:
                    print("Invalidating session due to upload error.")
                    self.session_id = None
                return False

        except Exception as e:
            print(f"❌ Upload exception: {e}")
            return False
