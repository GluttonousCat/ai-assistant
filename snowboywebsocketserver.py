#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import asyncio
import threading
import wave
import json
import os
import time
import traceback
import urllib.parse
import queue
import random
from pathlib import Path
import subprocess
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Callable

import numpy as np
import sounddevice as sd
import opuslib
import websockets
import requests
import soxr

try:
    from websockets.protocol import State
except ImportError:
    class State:
        OPEN = 1
        CLOSED = 3


_DEBUG_LOG_PATH = Path(__file__).resolve().parent / 'logs' / 'websocket_debug.log'
try:
    _DEBUG_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
except Exception:
    pass


def _log(msg: str):
    try:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        try:
            with open(_DEBUG_LOG_PATH, 'a', encoding='utf-8') as f:
                f.write(line + "\n")
        except Exception:
            pass
    except Exception:
        pass

# ================= 配置与工具部分 =================

class Config:
    def __init__(self):
        # WebSocket / HTTP 配置
        self.ws_url = os.getenv('WS_URL', 'wss://gspast.inspures.com/ws/xiaozhi/v1/')
        self.api_url = os.getenv('API_URL', 'https://gspast.inspures.com/api/chat/upload-audio')

        self.preset_role_id = os.getenv('PRESET_ROLE_ID', os.getenv('ROLE_ID', ''))
        self.preset_cache_dir = os.getenv(
            'PRESET_CACHE_DIR',
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)), 'preset_cache')
        )
        self.preset_warmup_sync_count = int(os.getenv(
            'PRESET_WARMUP_SYNC_COUNT', '1'
        ) or '1')

        self.input_sample_rate = 16000
        self.output_sample_rate = 24000
        self.channels = 1
        self.frame_duration = 60  # ms

        self.dong_sound_file = os.getenv(
            'DONG_SOUND_FILE',
            str(Path(__file__).resolve().parent / 'dong.wav')
        )

        self.input_frame_size = int(self.input_sample_rate * (self.frame_duration / 1000))
        self.output_frame_size = int(self.output_sample_rate * (self.frame_duration / 1000))


config = Config()

# 全局变量
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


# ================= 核心客户端类 =================

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
        self.loop = None  # asyncio loop引用
        self._reconnecting_lock = asyncio.Lock()  # 防止并发重连

        # --- 音频设备参数 ---
        self.input_device_id = None
        self.output_device_id = None

        # 【音量增强】默认 1.5 倍
        self.output_volume = 1.5

        # 实际硬件采样率 (初始化时检测)
        self.device_input_sample_rate = config.input_sample_rate
        self.device_output_sample_rate = 44100 # 默认值，会被实际硬件覆盖
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
        #    Level 1: _output_buffer (Queue) - 存放解码后的 24k 音频块
        #    Level 2: _resample_output_buffer (deque) - 存放重采样后待播放的采样点
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

    # 【新增】一个全版本兼容的连接状态检查属性
    @property
    def is_ws_connected(self) -> bool:
        """兼容所有 websockets 版本的连接状态检查"""
        if self.websocket is None:
            return False

        # 1. 优先尝试 state 属性 (websockets 10.0+ / 14.0+)
        # state 是一个 Enum, State.OPEN 的值通常是 1
        state = getattr(self.websocket, 'state', None)
        if state is not None:
            # 兼容 state 是 Enum 或 Int 的情况
            return state == State.OPEN or state == 1

        # 2. 回退尝试 closed 属性 (websockets < 10.0)
        # 如果没有 state 属性，尝试用 closed
        is_closed = getattr(self.websocket, 'closed', True)
        return not is_closed

    def load_local_preset_cache(self):
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
        try:
            threading.Thread(target=self._warmup_preset_pcm_cache, daemon=True).start()
        except Exception:
            return

    def _warmup_preset_pcm_cache(self):
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

    def warmup_preset_pcm_cache_sync(self, count: int = 1):
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

    async def initialize_audio(self):
        """初始化音频设备、编解码器及重采样器"""
        try:
            # 1. 自动检测设备能力
            self._detect_device_capabilities()

            # 2. 创建 Opus 编解码器
            # 录音: 16k VOIP
            self.opus_encoder = opuslib.Encoder(
                config.input_sample_rate,
                config.channels,
                opuslib.APPLICATION_VOIP,
            )
            # 播放: 24k (接收 Java 发来的高音质流)
            self.opus_decoder = opuslib.Decoder(
                config.output_sample_rate,
                config.channels
            )

            # 3. 创建输出重采样器 (24000 -> 硬件采样率)
            # 即使 Java 已经转成了 24k，但树莓派/Windows 声卡通常是 48k/44.1k
            # 为了最好的音质和防止沙哑，必须在这里再次重采样
            if self.device_output_sample_rate != config.output_sample_rate:
                print(f"🔧 初始化高质量重采样: {config.output_sample_rate}Hz -> {self.device_output_sample_rate}Hz")
                self.output_resampler = soxr.ResampleStream(
                    config.output_sample_rate,  # 输入: 24000
                    self.device_output_sample_rate,  # 输出: 44100/48000
                    num_channels=1,
                    dtype="float32",
                    quality="QQ"
                )
            else:
                self.output_resampler = None

            # 4. 创建音频流 (使用设备原生参数)
            output_block_size = int(self.device_output_sample_rate * (config.frame_duration / 1000))

            # 仅创建输出流 (输入流按需在 start_recording 时创建，或在此处创建但不启动)
            # 为了稳定，这里只启动输出流
            self.input_stream = None

            print(f"🔊 输出流配置: Rate={self.device_output_sample_rate}Hz, Block={output_block_size}")
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
            print(f"❌ 初始化音频设备失败: {e}")
            raise # 严重错误直接抛出

    def _server_http_base(self):
        try:
            parsed = urllib.parse.urlparse(config.api_url)
            return f"{parsed.scheme}://{parsed.netloc}"
        except Exception:
            return "http://127.0.0.1:8091"

    def notify_server_interrupt(self, reason: str = "wake_word"):
        try:
            if not self.session_id:
                return
            base = self._server_http_base()
            url = f"{base}/api/chat/interrupt"
            requests.post(url, params={'sessionId': self.session_id, 'reason': reason}, timeout=1)
        except Exception:
            return

    def _http_get_json(self, url, params=None, timeout=10):
        try:
            resp = requests.get(url, params=params, timeout=timeout)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def _download_file(self, url, target_path: Path, timeout=20):
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

    def prefetch_preset_audios(self):
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
        print(f"\U0001f4e6 Preset cache ready. roleId={role_id if role_id else 'AUTO'} voiceDir={voice_dir} files={len(cached)}")
        self._warmup_preset_pcm_cache_async()

    def _load_wav_float32(self, file_path: str):
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

    async def play_random_preset(self) -> float:
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

    def enqueue_beep(self, freq_hz: float = 880.0, duration_ms: int = 180, volume: float = 0.18) -> float:
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

    def get_preset_status(self):
        return {
            'cached_count': len(self._preset_items) if self._preset_items else 0,
            'last_error': self._last_preset_error,
            'last_error_detail': self._last_preset_error_detail,
            'chosen': self._last_preset_chosen,
        }

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
        # 1. 清空一级 Queue
        try:
            while not self._output_buffer.empty():
                self._output_buffer.get_nowait()
        except queue.Empty:
            pass

        # 2. 清空二级 Deque (重采样缓冲)
        self._resample_output_buffer.clear()
        print("🛑 收到唤醒指令，播放流已强制中断 (Buffer Cleared)")

    # ------------------ 音频回调 (SoundDevice 线程) ------------------

    # 【服务端不处理输入逻辑】
    def _input_callback(self, indata, frames, time_info, status):
        pass

    def _output_callback(self, outdata, frames, time_info, status):
        """输出回调 - 负责重采样、音量调节与混音"""
        if status and "underflow" not in str(status).lower():
            pass

        try:
            # 阶段 1: 填充缓冲区
            # 从 output_buffer (24k) 拉取数据，经过 soxr 重采样成 device_rate (如 48k)，放入 resample_buffer
            while len(self._resample_output_buffer) < frames:
                try:
                    audio_packet = self._output_buffer.get_nowait()
                    if self.output_resampler:
                        # 实时流重采样
                        resampled_data = self.output_resampler.resample_chunk(audio_packet, last=False)
                        if len(resampled_data) > 0:
                            self._resample_output_buffer.extend(resampled_data)
                    else:
                        self._resample_output_buffer.extend(audio_packet)
                except queue.Empty:
                    break

            # 阶段 2: 消费缓冲区
            # 从 resample_buffer 中取出指定数量的 frame 填充到声卡
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

    # ------------------ 音频操作 (Asyncio 线程) ------------------

    async def write_audio(self, opus_data: bytes):
        """解码(24k) -> 放入播放队列"""
        try:
            # 1. 解码：得到 24000Hz PCM
            pcm_data = self.opus_decoder.decode(opus_data, config.output_frame_size)
            audio_int16 = np.frombuffer(pcm_data, dtype=np.int16)
            audio_float = audio_int16.astype(np.float32) / 32768.0

            # 2. 入队 (重采样在 output_callback 中即时进行)
            try:
                self._output_buffer.put_nowait(audio_float)
            except queue.Full:
                print("⚠️ 播放队列已满，丢弃音频帧")

        except opuslib.OpusError as e:
            print(f"Opus decode error: {e}")
        except Exception as e:
            print(f"Write audio error: {e}")

    # =============服务端不处理输入逻辑=============
    async def start_recording(self):
        pass

    async def stop_recording(self) -> Optional[str]:
        pass
    # ==========================================

    # ------------------ 网络通信逻辑 ------------------

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
                "sample_rate": config.output_sample_rate, # 24000
                "channels": config.channels,
                "frame_duration": config.frame_duration,
            },
        }
        # 【修改】使用兼容的 is_ws_connected 属性
        if self.is_ws_connected:
            await self.websocket.send(json.dumps(hello_message))
        else:
            raise ConnectionError("Cannot say hello: Websocket not open")

    async def handle_server_hello(self, data: dict):
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
            # 【修改】使用兼容的 is_ws_connected 属性
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
        # 【修改】使用兼容的 is_ws_connected 属性
        is_socket_alive = self.is_ws_connected
        is_session_valid = self.session_id is not None

        if is_socket_alive and is_session_valid:
            return True

        print(f"⚠️ Connection check failed (Socket={is_socket_alive}, Session={is_session_valid}). Restoring...")
        await self.reconnect()

        # 【修改】使用兼容的 is_ws_connected 属性
        if self.is_ws_connected and self.session_id:
            return True
        return False

    async def send_recording_to_api(self, filename):
        """上传录音文件，上传前强制检查连接"""
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


# ================= HTTP Server =================

class MyHttpHandler(BaseHTTPRequestHandler):
    client: AudioClient = None

    def do_GET(self):
        parsed_path = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed_path.query)

        if parsed_path.path == '/do_send':
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

        elif parsed_path.path == '/record_start':
            # 兼容性保留
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Recording started (dummy)")

        elif parsed_path.path == '/record_stop':
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Recording stopped (dummy)")


        elif parsed_path.path == '/interrupt_play':
            if self.client:
                self.client.clear_audio_queue()
                try:
                    self.client.notify_server_interrupt()
                except Exception:
                    pass
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"Playback interrupted")

        elif parsed_path.path == '/play_preset':
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

        elif parsed_path.path == '/beep_ready':
            if not self.client:
                self.send_error(500, "No client")
                return
            try:
                file_param = params.get('file', [""])[0]
                env_file = os.environ.get('KWS_READY_SOUND_FILE', '')
                file_path = (file_param or env_file or '').strip()
                if not file_path:
                    try:
                        default_bootup = Path(__file__).resolve().parent / 'bootup.wav'
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

        elif parsed_path.path == '/set_volume':
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

        else:
            self.send_error(404)


# ================= Main =================

async def websocket_server(websocket, path):
    try:
        async for message in websocket:
            await websocket.send("Echo")
    except:
        pass


async def main():
    # 1. 创建客户端
    client = AudioClient()
    client.loop = asyncio.get_running_loop()
    try:
        client._load_dong_sound()
    except Exception:
        pass

    _log(f"boot ws_url={getattr(config, 'ws_url', '')} api_url={getattr(config, 'api_url', '')}")

    # 2. 初始化音频
    try:
        await client.initialize_audio()
    except Exception as e:
        _log(f"initialize_audio failed: {type(e).__name__}:{e!r}")
        _log(traceback.format_exc())
        raise

    try:
        client.prefetch_preset_audios()
    except Exception:
        pass

    try:
        client.warmup_preset_pcm_cache_sync(config.preset_warmup_sync_count)
    except Exception:
        pass

    # 3. 注入 HTTP Handler
    MyHttpHandler.client = client

    # 4. 启动本地 WS Server (可选)
    _log(f"Starting Local WS Server on {WS_PORT}")
    try:
        ws_server = await websockets.serve(websocket_server, "0.0.0.0", WS_PORT)
    except Exception as e:
        _log(f"websockets.serve failed: {type(e).__name__}:{e!r}")
        _log(traceback.format_exc())
        raise

    # 5. 启动 HTTP Server (线程)
    def run_http_server():
        try:
            http_server = HTTPServer(('0.0.0.0', HTTP_PORT), MyHttpHandler)
            _log(f"HTTP server started on port {HTTP_PORT}")
            http_server.serve_forever()
        except Exception as e:
            _log(f"HTTP server failed: {type(e).__name__}:{e!r}")
            _log(traceback.format_exc())
            raise

    threading.Thread(target=run_http_server, daemon=True).start()

    # 6. 初始连接
    client.handing_message_task = asyncio.create_task(client.reconnect())

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("Stopping...")
        if client.input_stream: client.input_stream.stop()
        if client.output_stream: client.output_stream.stop()


if __name__ == "__main__":
    asyncio.run(main())
